# plugins/rift_vessel/minecraft/wiki_client.py
"""Live Minecraft wiki client with an incremental on-disk cache.

Replaces the old hand-written ``wiki/knowledge.json`` snippets (a handful of
incomplete, static facts) with the *real* game wiki, consulted on demand:

* :func:`search_wiki` queries the public `minecraft.wiki
  <https://minecraft.wiki>`_ MediaWiki API for page titles matching a query.
* :func:`fetch_page_plaintext` pulls a page's plaintext extract.
* :func:`lookup` ties them together: search → fetch top pages → (once) distil a
  short factual summary via the active Cortex engine → cache the result to
  disk. Subsequent look-ups of the same page read the cached summary, so the
  network + LLM cost is paid **once per page**.

Design constraints (see ``docs/rift_vessel.rst`` and ``AGENTS.md`` §5c):

* **Reference, not a script.** The wiki tells Synth how the world *works*; it
  never tells it *what to do*. No catalogue, no quest templates.
* **Keyword-free.** The query is structural game tokens (block/item ids), never
  natural-language phrase matching.
* **Best-effort / fail-safe.** Every network and LLM call is guarded; offline or
  on any error the client degrades to whatever is already cached (possibly
  nothing) and never raises into the caller. This keeps the Vessel's Fast-Lane
  ``lookup_knowledge`` verb from ever breaking a beat.
* **No new heavy dependency.** Uses ``aiohttp`` (already a project dep) with a
  tight timeout.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_warning

LOG_PREFIX = "[minecraft_wiki]"

_API_URL = "https://minecraft.wiki/api.php"
_PAGE_URL = "https://minecraft.wiki/w/"
_USER_AGENT = "SynthRiftVessel/1.0 (minecraft knowledge lookup)"

# Cache lives next to this module, under wiki/cache/. One JSON file per page.
_CACHE_DIR = Path(__file__).resolve().parent / "wiki" / "cache"


# ---------------------------------------------------------------------------
# Config helpers (all fail-safe, keyword-free)
# ---------------------------------------------------------------------------
def _cfg_bool(key: str, default: bool) -> bool:
    try:
        return bool(
            config_registry.get_value(
                key, default, group="plugins", component="vessel_plugin"
            )
        )
    except Exception:  # pragma: no cover - defensive
        return default


def _cfg_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(
            config_registry.get_value(
                key, default, group="plugins", component="vessel_plugin"
            )
        )
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def is_live_fetch_enabled() -> bool:
    """Whether the client may hit the network (default True)."""
    return _cfg_bool("VESSEL_KNOWLEDGE_LIVE_FETCH", True)


def fetch_timeout_sec() -> float:
    """Per-request HTTP timeout in seconds (default 4, clamped 1..30)."""
    return float(_cfg_int("VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC", 4, 1, 30))


def summary_max_chars() -> int:
    """Max length of a distilled page summary (default 600, clamped 120..4000)."""
    return _cfg_int("VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS", 600, 120, 4000)


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _slug(title: str) -> str:
    """Return a filesystem-safe slug for a page title (keyword-free, structural)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    return s or "page"


def _cache_path(title: str) -> Path:
    return _CACHE_DIR / f"{_slug(title)}.json"


def _read_cache(title: str) -> dict[str, Any] | None:
    """Return a cached page record, or ``None`` if absent/unreadable."""
    path = _cache_path(title)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} cache read failed for {title!r}: {exc}")
        return None


def _write_cache(record: dict[str, Any]) -> None:
    """Persist a page record. Best-effort — a failure is logged, never raised."""
    title = str(record.get("title") or "")
    if not title:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(title).write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} cache write failed for {title!r}: {exc}")


# ---------------------------------------------------------------------------
# Live MediaWiki API
# ---------------------------------------------------------------------------
async def search_wiki(query: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` matching page titles for ``query``.

    Uses the MediaWiki search endpoint. Best-effort: returns ``[]`` on any
    error, when live fetch is disabled, or when the query is empty.
    """
    q = str(query or "").strip()
    if not q or not is_live_fetch_enabled():
        return []
    lim = max(1, min(10, int(limit) if isinstance(limit, int) else 3))
    params = {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "srlimit": str(lim),
        "format": "json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=fetch_timeout_sec())
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": _USER_AGENT}
        ) as session:
            async with session.get(_API_URL, params=params) as resp:
                if resp.status != 200:
                    log_debug(f"{LOG_PREFIX} search HTTP {resp.status} for {q!r}")
                    return []
                data = await resp.json()
        hits = data.get("query", {}).get("search", []) if isinstance(data, dict) else []
        titles = [
            str(h.get("title")) for h in hits if isinstance(h, dict) and h.get("title")
        ]
        return titles[:lim]
    except Exception as exc:  # pragma: no cover - network/offline
        log_debug(f"{LOG_PREFIX} search failed for {q!r}: {exc}")
        return []


async def fetch_page_plaintext(title: str, max_chars: int = 6000) -> str:
    """Return the plaintext extract of a wiki page, or ``""`` on any failure."""
    t = str(title or "").strip()
    if not t or not is_live_fetch_enabled():
        return ""
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": t,
        "format": "json",
        "redirects": "1",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=fetch_timeout_sec())
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": _USER_AGENT}
        ) as session:
            async with session.get(_API_URL, params=params) as resp:
                if resp.status != 200:
                    log_debug(f"{LOG_PREFIX} fetch HTTP {resp.status} for {t!r}")
                    return ""
                data = await resp.json()
        pages = data.get("query", {}).get("pages", {}) if isinstance(data, dict) else {}
        for _pid, page in (pages or {}).items():
            if isinstance(page, dict):
                extract = str(page.get("extract") or "").strip()
                if extract:
                    return extract[: max(0, int(max_chars))]
        return ""
    except Exception as exc:  # pragma: no cover - network/offline
        log_debug(f"{LOG_PREFIX} fetch failed for {t!r}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# One-shot LLM summary (paid once per page, then cached)
# ---------------------------------------------------------------------------
async def _summarize(title: str, extract: str) -> str:
    """Distil a page extract into a short factual note via the Cortex engine.

    Runs once per page (the result is cached). Best-effort: on any failure —
    engine unavailable, generation error, offline — it falls back to a plain
    truncation of the extract so a note is still returned. The summary is kept
    in English (the wiki's language) since it is internal reference material.
    """
    limit = summary_max_chars()
    text = str(extract or "").strip()
    if not text:
        return ""
    try:
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry

        active = await get_active_cortex_engine(scope="vessel")
        registry = get_cortex_registry()
        engine = registry.get_engine(active) or registry.load_engine(active)
        if engine is None or not hasattr(engine, "generate_response"):
            raise RuntimeError("no cortex engine available")

        prompt = (
            "You are distilling a Minecraft wiki page into a compact factual "
            "reference note for a game-playing agent. State only how the game "
            "works — required tools, recipes, prerequisites, hazards. Do NOT "
            "suggest goals or what to do. Be terse and factual, at most "
            f"{limit} characters, plain text (no markdown, no bullets).\n\n"
            f"Page: {title}\n\n{text[:4000]}"
        )
        result = await engine.generate_response(prompt)
        summary = str(result or "").strip()
        if summary:
            return summary[:limit]
    except Exception as exc:  # pragma: no cover - engine/offline
        log_debug(f"{LOG_PREFIX} summary failed for {title!r}: {exc}")
    # Fallback: plain truncation of the raw extract.
    return text[:limit]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def lookup(
    query: str, limit: int = 3, *, cache_only: bool = False
) -> list[dict[str, Any]]:
    """Return up to ``limit`` knowledge notes relevant to ``query``.

    Each note is ``{"title", "text", "url"}`` — the same shape the old curated
    KB returned and the shape :func:`core.vessel_beat._fmt_knowledge` expects,
    so the prompt-rendering layer is unchanged.

    Flow: search the wiki for matching titles → for each, serve a cached summary
    if present, else fetch + summarise + cache. Fully fail-safe: offline / on
    any error the client returns whatever it could gather from cache (possibly
    an empty list). Never raises.

    When ``cache_only`` is set (used by the automatic will-beat path so the beat
    stays fast and offline-safe), the client never hits the network or the LLM —
    it returns only pages already present in the local cache.
    """
    q = str(query or "").strip()
    if not q:
        return []
    lim = max(1, min(10, int(limit) if isinstance(limit, int) else 3))

    if cache_only:
        titles = _cached_titles_matching(q, lim)
    else:
        titles = await search_wiki(q, limit=lim)
        if not titles:
            # Offline / no live fetch: fall back to any cached page whose slug
            # overlaps a query token (structural, keyword-free).
            titles = _cached_titles_matching(q, lim)

    notes: list[dict[str, Any]] = []
    for title in titles[:lim]:
        cached = _read_cache(title)
        if cached and cached.get("summary"):
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
    return notes


def _cached_titles_matching(query: str, limit: int) -> list[str]:
    """Return cached page titles whose slug shares a token with ``query``.

    Offline fallback only — matches on structural tokens (block/item ids), never
    natural-language keywords.
    """
    tokens = {tok for tok in _slug(query).split("_") if tok}
    if not tokens:
        return []
    matches: list[str] = []
    try:
        if not _CACHE_DIR.is_dir():
            return []
        for path in _CACHE_DIR.glob("*.json"):
            stem_tokens = set(path.stem.split("_"))
            if tokens & stem_tokens:
                rec = _read_cache_by_path(path)
                if rec and rec.get("title"):
                    matches.append(str(rec["title"]))
            if len(matches) >= limit:
                break
    except Exception as exc:  # pragma: no cover - defensive
        log_warning(f"{LOG_PREFIX} cache scan failed: {exc}")
    return matches[:limit]


def _read_cache_by_path(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # pragma: no cover - defensive
        return None
