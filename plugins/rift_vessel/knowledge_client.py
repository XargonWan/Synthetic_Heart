# plugins/rift_vessel/knowledge_client.py
"""World-agnostic game-knowledge client for the Rift Vessel core.

This is the **mechanism** half of the Rift Vessel knowledge base (see
``AGENTS.md`` §5c and ``docs/rift_vessel.rst``). It knows *how* to look a fact
up — search, cache, distil, fall back — but nothing about *which* game or wiki
to consult. Each world adapter (Minecraft, a future Skyrim/VRChat, ...) supplies
its own :class:`WikiSource` descriptors; the client stays generic.

Design goals (per ``TODO - Rift Vessel.md`` §9 "evolve the lookup flow"):

* **Per-game wiki sources.** The source URLs live in a :class:`WikiSource`
  descriptor supplied by the adapter, never hardcoded here.
* **Local-first precedence.** Lookup order is
  ``local cache → per-game wiki(s) → generic web search``. The on-disk cache
  wins over any network call because it is already distilled and instant.
* **Fetch at most once.** Every result — from a wiki *or* the web fallback — is
  summarised once by the active Cortex engine and written back to the cache,
  keyed by a slug of the page/result title, so a fact is fetched at most once
  and later reuse (or an offline session) is served from disk.
* **Reference, not a script.** The summary prompt states only how the world
  *works*; it never suggests goals or what to do.
* **Keyword-free.** The query is structural game tokens (block/item ids), never
  natural-language phrase matching. Cache/offline matching is on slug tokens.
* **Best-effort / fail-safe.** Every network and LLM call is guarded; offline or
  on any error the client degrades to whatever is already cached (possibly an
  empty list) and never raises into the caller. This keeps the Vessel's
  Fast-Lane ``lookup_knowledge`` verb from ever breaking a beat.

The public entry point is :func:`lookup`. Each note it returns is
``{"title", "text", "url"}`` — the shape
:func:`core.vessel_beat._fmt_knowledge` expects, so the prompt-rendering layer
is unchanged.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_warning

LOG_PREFIX = "[vessel_knowledge]"

# Default summary-prompt template. ``{limit}``, ``{title}`` and ``{text}`` are
# substituted per page. A world may override the "game" wording via
# ``WikiSource.summary_prompt``; ``{game}`` is substituted into the default.
_DEFAULT_SUMMARY_PROMPT = (
    "You are distilling a {game} wiki page into a compact factual reference "
    "note for a game-playing agent. State only how the game works — required "
    "tools, recipes, prerequisites, hazards. Do NOT suggest goals or what to "
    "do. Be terse and factual, at most {limit} characters, plain text (no "
    "markdown, no bullets).\n\nPage: {title}\n\n{text}"
)


@dataclass
class WikiSource:
    """A per-game knowledge source descriptor (supplied by a world adapter).

    Everything world-specific about the knowledge base lives here so the client
    stays world-agnostic. A world declares one or more of these from its
    connector's ``get_knowledge_wiki_sources()`` hook.

    Attributes:
        name: A short structural id for the source (used only in logs).
        api_url: The MediaWiki ``api.php`` endpoint to search/fetch, or ``""``
            for a source with no MediaWiki API (web-search-only worlds).
        page_url: Base URL prefix for building a human page link
            (e.g. ``"https://minecraft.wiki/w/"``); a page title is appended.
        user_agent: HTTP ``User-Agent`` header sent on requests.
        game: Human game name substituted into the default summary prompt.
        summary_prompt: Optional full override of the summary prompt template;
            when set, ``{limit}``/``{title}``/``{text}`` are substituted and
            ``game`` is ignored.
    """

    name: str
    api_url: str = ""
    page_url: str = ""
    user_agent: str = "SynthRiftVessel/1.0"
    game: str = "game"
    summary_prompt: str = ""

    def build_summary_prompt(self, title: str, text: str, limit: int) -> str:
        template = self.summary_prompt or _DEFAULT_SUMMARY_PROMPT
        try:
            return template.format(
                game=self.game, limit=limit, title=title, text=text[:4000]
            )
        except (KeyError, IndexError, ValueError):  # pragma: no cover - defensive
            # A malformed custom template must never break a lookup.
            return _DEFAULT_SUMMARY_PROMPT.format(
                game=self.game, limit=limit, title=title, text=text[:4000]
            )


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
    """Whether the client may hit the network at all (default True)."""
    return _cfg_bool("VESSEL_KNOWLEDGE_LIVE_FETCH", True)


def is_web_fallback_enabled() -> bool:
    """Whether the generic web-search fallback is allowed (default True)."""
    return _cfg_bool("VESSEL_KNOWLEDGE_WEB_FALLBACK", True)


def fetch_timeout_sec() -> float:
    """Per-request HTTP timeout in seconds (default 4, clamped 1..30)."""
    return float(_cfg_int("VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC", 4, 1, 30))


def summary_max_chars() -> int:
    """Max length of a distilled page summary (default 600, clamped 120..4000)."""
    return _cfg_int("VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS", 600, 120, 4000)


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def slug(title: str) -> str:
    """Return a filesystem-safe slug for a page title (keyword-free, structural)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    return s or "page"


def _cache_path(cache_dir: Path, title: str) -> Path:
    return cache_dir / f"{slug(title)}.json"


def read_cache(cache_dir: Path, title: str) -> dict[str, Any] | None:
    """Return a cached page record, or ``None`` if absent/unreadable."""
    path = _cache_path(cache_dir, title)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} cache read failed for {title!r}: {exc}")
        return None


def write_cache(cache_dir: Path, record: dict[str, Any]) -> None:
    """Persist a page record. Best-effort — a failure is logged, never raised."""
    title = str(record.get("title") or "")
    if not title:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(cache_dir, title).write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} cache write failed for {title!r}: {exc}")


def _read_cache_by_path(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # pragma: no cover - defensive
        return None


def cached_titles_matching(cache_dir: Path, query: str, limit: int) -> list[str]:
    """Return cached page titles whose slug shares a token with ``query``.

    Offline / cache-first matching only — matches on structural tokens
    (block/item ids), never natural-language keywords.
    """
    tokens = {tok for tok in slug(query).split("_") if tok}
    if not tokens:
        return []
    matches: list[str] = []
    try:
        if not cache_dir.is_dir():
            return []
        for path in cache_dir.glob("*.json"):
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


# ---------------------------------------------------------------------------
# Live MediaWiki API (generic — the endpoint comes from the WikiSource)
# ---------------------------------------------------------------------------
async def search_wiki(source: WikiSource, query: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` matching page titles for ``query`` on ``source``.

    Uses the MediaWiki search endpoint. Best-effort: returns ``[]`` on any
    error, when live fetch is disabled, when the source has no API, or when the
    query is empty.
    """
    q = str(query or "").strip()
    if not q or not source.api_url or not is_live_fetch_enabled():
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
            timeout=timeout, headers={"User-Agent": source.user_agent}
        ) as session:
            async with session.get(source.api_url, params=params) as resp:
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


async def fetch_page_plaintext(
    source: WikiSource, title: str, max_chars: int = 6000
) -> str:
    """Return the plaintext extract of a wiki page, or ``""`` on any failure."""
    t = str(title or "").strip()
    if not t or not source.api_url or not is_live_fetch_enabled():
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
            timeout=timeout, headers={"User-Agent": source.user_agent}
        ) as session:
            async with session.get(source.api_url, params=params) as resp:
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


def _page_url(source: WikiSource, title: str) -> str:
    if not source.page_url:
        return ""
    return source.page_url + str(title).replace(" ", "_")


# ---------------------------------------------------------------------------
# Generic web-search fallback (reuses the project's web_search stack)
# ---------------------------------------------------------------------------
async def web_search(query: str, limit: int = 3) -> list[dict[str, str]]:
    """Return up to ``limit`` generic web results as ``{title, text, url}``.

    Last-resort source when no declared wiki matched. Delegates to the existing
    ``plugins.web_search`` stack (SearXNG → Tavily). Best-effort: returns ``[]``
    when live fetch or the web fallback is disabled, when the stack is not
    configured, or on any error. The raw snippet/page text is returned as
    ``text``; the caller summarises + caches it like a wiki page.
    """
    q = str(query or "").strip()
    if not q or not is_live_fetch_enabled() or not is_web_fallback_enabled():
        return []
    lim = max(1, min(10, int(limit) if isinstance(limit, int) else 3))
    try:
        from plugins.web_search.search_engine import collect_valid_results

        results = await collect_valid_results(q, min_valid=lim)
    except Exception as exc:  # pragma: no cover - network/offline/not configured
        log_debug(f"{LOG_PREFIX} web fallback failed for {q!r}: {exc}")
        return []
    notes: list[dict[str, str]] = []
    for hit in results[:lim]:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        url = str(hit.get("url") or "").strip()
        text = str(hit.get("page") or hit.get("snippet") or "").strip()
        if not title or not text:
            continue
        notes.append({"title": title, "text": text, "url": url})
    return notes


# ---------------------------------------------------------------------------
# One-shot LLM summary (paid once per page, then cached)
# ---------------------------------------------------------------------------
async def summarize(source: WikiSource, title: str, extract: str) -> str:
    """Distil a page/result extract into a short factual note via Cortex.

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

        prompt = source.build_summary_prompt(title, text, limit)
        result = await engine.generate_response(prompt)
        summary = str(result or "").strip()
        if summary:
            return summary[:limit]
    except Exception as exc:  # pragma: no cover - engine/offline
        log_debug(f"{LOG_PREFIX} summary failed for {title!r}: {exc}")
    # Fallback: plain truncation of the raw extract.
    return text[:limit]


# ---------------------------------------------------------------------------
# Public entry point — local-first, multi-source lookup
# ---------------------------------------------------------------------------
async def lookup(
    cache_dir: Path,
    sources: list[WikiSource],
    query: str,
    limit: int = 3,
    *,
    cache_only: bool = False,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` knowledge notes relevant to ``query``.

    Local-first precedence (``TODO - Rift Vessel.md`` §9):

    1. **Local cache** — always tried first, offline-safe and instant. When
       ``cache_only`` is set (the automatic will/action-beat path) the lookup
       stops here: it never touches the network or the LLM.
    2. **Per-game wiki(s)** — each declared :class:`WikiSource` is searched in
       order; the first with matching pages wins. Each page is fetched,
       summarised once, and cached.
    3. **Generic web search** — a last-resort fallback when no wiki matched;
       results are summarised + cached exactly like a wiki page.

    Every result from any tier is written back to ``cache_dir`` keyed by a slug
    of its title, so a fact is fetched at most once. Each note is
    ``{"title", "text", "url"}``. Fully fail-safe: offline / on any error the
    client returns whatever it could gather from cache. Never raises.
    """
    q = str(query or "").strip()
    if not q:
        return []
    lim = max(1, min(10, int(limit) if isinstance(limit, int) else 3))

    # --- Tier 1: local cache (always first) --------------------------------
    notes: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for title in cached_titles_matching(cache_dir, q, lim):
        rec = read_cache(cache_dir, title)
        if not (rec and rec.get("summary")):
            continue
        s = slug(rec.get("title", title))
        if s in seen_slugs:
            continue
        seen_slugs.add(s)
        notes.append(
            {
                "title": rec.get("title", title),
                "text": rec.get("summary", ""),
                "url": rec.get("url", ""),
            }
        )
        if len(notes) >= lim:
            return notes

    if cache_only:
        return notes

    remaining = lim - len(notes)
    if remaining <= 0:
        return notes

    # --- Tier 2: per-game wiki(s), in declared order -----------------------
    for source in sources or []:
        if remaining <= 0:
            break
        titles = await search_wiki(source, q, limit=remaining)
        for title in titles:
            if remaining <= 0:
                break
            s = slug(title)
            if s in seen_slugs:
                continue
            cached = read_cache(cache_dir, title)
            if cached and cached.get("summary"):
                seen_slugs.add(s)
                notes.append(
                    {
                        "title": cached.get("title", title),
                        "text": cached.get("summary", ""),
                        "url": cached.get("url", _page_url(source, title)),
                    }
                )
                remaining -= 1
                continue
            extract = await fetch_page_plaintext(source, title)
            if not extract:
                continue
            summary = await summarize(source, title, extract)
            if not summary:
                continue
            url = _page_url(source, title)
            seen_slugs.add(s)
            write_cache(
                cache_dir,
                {
                    "title": title,
                    "url": url,
                    "raw_extract": extract,
                    "summary": summary,
                    "fetched_at": time.time(),
                },
            )
            notes.append({"title": title, "text": summary, "url": url})
            remaining -= 1

    if remaining <= 0 or notes:
        # A wiki produced something (or filled the quota) — do not fall through
        # to the generic web search. The web fallback is only for the case where
        # *no* declared wiki knew the answer.
        if notes:
            return notes

    # --- Tier 3: generic web-search fallback (last resort) -----------------
    web_source = sources[0] if sources else WikiSource(name="web")
    for hit in await web_search(q, limit=remaining):
        if remaining <= 0:
            break
        title = hit.get("title", "")
        s = slug(title)
        if not title or s in seen_slugs:
            continue
        summary = await summarize(web_source, title, hit.get("text", ""))
        if not summary:
            continue
        url = hit.get("url", "")
        seen_slugs.add(s)
        write_cache(
            cache_dir,
            {
                "title": title,
                "url": url,
                "raw_extract": hit.get("text", ""),
                "summary": summary,
                "fetched_at": time.time(),
            },
        )
        notes.append({"title": title, "text": summary, "url": url})
        remaining -= 1

    return notes
