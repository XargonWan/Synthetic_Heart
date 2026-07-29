# plugins/rift_vessel/minecraft/wiki_client.py
"""Minecraft adapter over the world-agnostic Vessel knowledge client.

The generic lookup mechanism — MediaWiki search/fetch, one-time LLM summary,
on-disk cache, local-first multi-source precedence, and the generic web-search
fallback — now lives in :mod:`plugins.rift_vessel.knowledge_client` so it can be
reused by any world adapter. This module is the **Minecraft source descriptor**:
it declares the live `minecraft.wiki <https://minecraft.wiki>`_ MediaWiki
endpoint and keeps a stable public surface (``search_wiki``,
``fetch_page_plaintext``, ``_summarize``, ``lookup``, ``_CACHE_DIR``,
``_PAGE_URL``, ``_slug``) so the connector and tests can monkeypatch the
Minecraft primitives without touching the core client.

Design constraints (see ``docs/rift_vessel.rst`` and ``AGENTS.md`` §5c):

* **Reference, not a script.** The wiki tells Synth how the world *works*; it
  never tells it *what to do*. No catalogue, no quest templates.
* **Keyword-free.** The query is structural game tokens (block/item ids), never
  natural-language phrase matching.
* **Best-effort / fail-safe.** Every network and LLM call is guarded; offline or
  on any error the client degrades to whatever is already cached (possibly
  nothing) and never raises into the caller.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from plugins.rift_vessel import knowledge_client as _kc
from plugins.rift_vessel.knowledge_client import WikiSource

LOG_PREFIX = "[minecraft_wiki]"

_API_URL = "https://minecraft.wiki/api.php"
_PAGE_URL = "https://minecraft.wiki/w/"
_USER_AGENT = "SynthRiftVessel/1.0 (minecraft knowledge lookup)"

# Cache lives next to this module, under wiki/cache/. One JSON file per page.
_CACHE_DIR = Path(__file__).resolve().parent / "wiki" / "cache"

#: The Minecraft knowledge source consumed by the core client. Built once and
#: reused; the core client is fully parameterised by this descriptor.
MINECRAFT_WIKI_SOURCE = WikiSource(
    name="minecraft.wiki",
    api_url=_API_URL,
    page_url=_PAGE_URL,
    user_agent=_USER_AGENT,
    game="Minecraft",
)


# ---------------------------------------------------------------------------
# Config helpers — thin re-exports of the core client (kept for compatibility)
# ---------------------------------------------------------------------------
def is_live_fetch_enabled() -> bool:
    """Whether the client may hit the network (default True)."""
    return _kc.is_live_fetch_enabled()


def fetch_timeout_sec() -> float:
    """Per-request HTTP timeout in seconds (default 4, clamped 1..30)."""
    return _kc.fetch_timeout_sec()


def summary_max_chars() -> int:
    """Max length of a distilled page summary (default 600, clamped 120..4000)."""
    return _kc.summary_max_chars()


# ---------------------------------------------------------------------------
# Disk cache — thin re-exports bound to this module's ``_CACHE_DIR``
# ---------------------------------------------------------------------------
def _slug(title: str) -> str:
    """Return a filesystem-safe slug for a page title (keyword-free, structural)."""
    return _kc.slug(title)


def _cache_path(title: str) -> Path:
    return _CACHE_DIR / f"{_slug(title)}.json"


def _read_cache(title: str) -> dict[str, Any] | None:
    """Return a cached page record, or ``None`` if absent/unreadable."""
    return _kc.read_cache(_CACHE_DIR, title)


def _write_cache(record: dict[str, Any]) -> None:
    """Persist a page record. Best-effort — a failure is logged, never raised."""
    _kc.write_cache(_CACHE_DIR, record)


# ---------------------------------------------------------------------------
# Live MediaWiki API — thin delegates bound to the Minecraft source
# ---------------------------------------------------------------------------
async def search_wiki(query: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` matching minecraft.wiki page titles for ``query``.

    Best-effort: returns ``[]`` on any error, when live fetch is disabled, or
    when the query is empty. Delegates to the core client with the Minecraft
    :class:`~plugins.rift_vessel.knowledge_client.WikiSource`.
    """
    return await _kc.search_wiki(MINECRAFT_WIKI_SOURCE, query, limit=limit)


async def fetch_page_plaintext(title: str, max_chars: int = 6000) -> str:
    """Return the plaintext extract of a minecraft.wiki page, or ``""``."""
    return await _kc.fetch_page_plaintext(
        MINECRAFT_WIKI_SOURCE, title, max_chars=max_chars
    )


# ---------------------------------------------------------------------------
# One-shot LLM summary (paid once per page, then cached)
# ---------------------------------------------------------------------------
async def _summarize(title: str, extract: str) -> str:
    """Distil a minecraft.wiki page extract into a short factual note.

    Runs once per page (the result is cached). Best-effort: on any failure it
    falls back to a plain truncation of the extract so a note is still returned.
    """
    return await _kc.summarize(MINECRAFT_WIKI_SOURCE, title, extract)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def lookup(
    query: str, limit: int = 3, *, cache_only: bool = False
) -> list[dict[str, Any]]:
    """Return up to ``limit`` Minecraft knowledge notes relevant to ``query``.

    Each note is ``{"title", "text", "url"}`` — the shape
    :func:`core.vessel_beat._fmt_knowledge` expects, so the prompt-rendering
    layer is unchanged.

    Local-first precedence (``TODO - Rift Vessel.md`` §9):
    ``local cache → minecraft.wiki → generic web search``. Every result from any
    tier is summarised once and written back to the cache, so a fact is fetched
    at most once. When ``cache_only`` is set (the automatic will/action-beat
    path) the lookup never hits the network or the LLM — it returns only pages
    already present in the local cache. Fully fail-safe; never raises.

    This method drives the lookup through this module's own primitives
    (``search_wiki`` / ``fetch_page_plaintext`` / ``_summarize``) so the
    connector and tests can monkeypatch the Minecraft-specific steps.
    """
    q = str(query or "").strip()
    if not q:
        return []
    lim = max(1, min(10, int(limit) if isinstance(limit, int) else 3))

    # --- Tier 1: local cache (always first) --------------------------------
    if cache_only:
        titles = _cached_titles_matching(q, lim)
    else:
        titles = await search_wiki(q, limit=lim)
        if not titles:
            # Offline / no live fetch / no wiki match: fall back to any cached
            # page whose slug overlaps a query token (structural, keyword-free).
            titles = _cached_titles_matching(q, lim)

    notes: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for title in titles[:lim]:
        s = _slug(title)
        if s in seen_slugs:
            continue
        cached = _read_cache(title)
        if cached and cached.get("summary"):
            seen_slugs.add(s)
            notes.append(
                {
                    "title": cached.get("title", title),
                    "text": cached.get("summary", ""),
                    "url": cached.get("url", _PAGE_URL + _slug(title)),
                }
            )
            continue
        if cache_only:
            continue
        extract = await fetch_page_plaintext(title)
        if not extract:
            continue
        summary = await _summarize(title, extract)
        if not summary:
            continue
        url = _PAGE_URL + str(title).replace(" ", "_")
        seen_slugs.add(s)
        _write_cache(
            {
                "title": title,
                "url": url,
                "raw_extract": extract,
                "summary": summary,
                "fetched_at": time.time(),
            }
        )
        notes.append({"title": title, "text": summary, "url": url})

    if notes or cache_only:
        return notes

    # --- Tier 3: generic web-search fallback (last resort) -----------------
    # No cached page and minecraft.wiki knew nothing. Try a generic web search
    # before giving up; results are summarised + cached like a wiki page.
    for hit in await _kc.web_search(q, limit=lim):
        title = hit.get("title", "")
        s = _slug(title)
        if not title or s in seen_slugs:
            continue
        summary = await _summarize(title, hit.get("text", ""))
        if not summary:
            continue
        url = hit.get("url", "")
        seen_slugs.add(s)
        _write_cache(
            {
                "title": title,
                "url": url,
                "raw_extract": hit.get("text", ""),
                "summary": summary,
                "fetched_at": time.time(),
            }
        )
        notes.append({"title": title, "text": summary, "url": url})
        if len(notes) >= lim:
            break
    return notes


def _cached_titles_matching(query: str, limit: int) -> list[str]:
    """Return cached page titles whose slug shares a token with ``query``.

    Offline / cache-first matching — matches on structural tokens (block/item
    ids), never natural-language keywords.
    """
    return _kc.cached_titles_matching(_CACHE_DIR, query, limit)
