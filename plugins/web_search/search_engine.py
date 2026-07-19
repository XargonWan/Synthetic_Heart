"""Web search engines and page fetching with a per-task shared fetch cache.

This module owns the single implementation of the search backends (SearXNG as the
primary self-hosted engine, with an optional Tavily API backend) and the
page-content fetcher. ``web_search_plugin`` imports these helpers so there is
exactly one implementation of each.

Note: the keyless DuckDuckGo backend was removed because DuckDuckGo serves bots a
CAPTCHA and blocks automated queries, so it returned no results in practice.

The :class:`FetchCache` is the mechanism that lets multiple concurrent queries of
the *same* search task avoid re-scraping the same URL: the first query to touch a
URL fetches it while others wait on a per-URL lock and reuse the cached result. A
query that finds a URL already in-flight/cached by a sibling query can therefore
move on to the next result instead of duplicating work.
"""

from __future__ import annotations

import asyncio

import requests
from bs4 import BeautifulSoup

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _tavily_api_key() -> str:
    """Read the Tavily API key from the config registry (empty -> disabled)."""
    try:
        return str(config_registry.get_value("TAVILY_API_KEY", "") or "").strip()
    except Exception:
        return ""


def _searxng_url() -> str:
    """Read the SearXNG base URL from the config registry (empty -> disabled)."""
    try:
        return (
            str(config_registry.get_value("SEARXNG_URL", "") or "").strip().rstrip("/")
        )
    except Exception:
        return ""


async def search_tavily(
    api_key: str, query: str, max_results: int = 5
) -> list[dict[str, str]]:
    """Search via the Tavily API. Returns an empty list on any failure."""

    def _do_post() -> dict:
        headers = {"Content-Type": "application/json"}
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "include_answer": False,
            "max_results": max_results,
        }
        response = requests.post(
            "https://api.tavily.com/search",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    try:
        data = await asyncio.to_thread(_do_post)
        results = data.get("results", [])
        log_info(f"[web_search] Tavily returned {len(results)} results")
        return [
            {
                "title": r.get("title", ""),
                "snippet": r.get("content", ""),
                "url": r.get("url", ""),
            }
            for r in results
        ]
    except Exception as e:
        log_warning(f"[web_search] Tavily API request failed: {e}")
        return []


async def search_searxng(
    base_url: str, query: str, max_results: int = 5, page: int = 1
) -> list[dict[str, str]]:
    """Search via a self-hosted SearXNG instance (JSON API).

    Queries ``{base_url}/search?format=json`` and maps the SearXNG result shape
    (``title`` / ``content`` / ``url``) to the plugin's ``{title, snippet, url}``
    shape. Returns an empty list on any failure so ``run_search`` can fall back to
    the next backend. The ``page`` argument enables paging past the first batch
    so callers can collect more *valid* results than a single page yields.
    """

    def _do_get() -> dict:
        headers = {"User-Agent": _USER_AGENT}
        params = {"q": query, "format": "json", "p": page}
        response = requests.get(
            f"{base_url}/search",
            headers=headers,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    try:
        data = await asyncio.to_thread(_do_get)
        results = data.get("results", [])[:max_results]
        mapped: list[dict[str, str]] = []
        for r in results:
            title = str(r.get("title") or "").strip()
            snippet = str(r.get("content") or "").strip()
            url = str(r.get("url") or "").strip()
            if title and url:
                mapped.append({"title": title, "snippet": snippet, "url": url})
        log_info(f"[web_search] SearXNG returned {len(mapped)} results")
        return mapped
    except Exception as e:
        log_warning(f"[web_search] SearXNG search failed: {e}")
        return []


async def run_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Run a single query on the active engine.

    Backend priority: SearXNG (self-hosted, if configured) -> Tavily (if keyed).
    SearXNG that yields no results falls through to Tavily. If neither backend is
    configured or both yield nothing, an empty list is returned. The DuckDuckGo
    backend was removed because it blocks bots with a CAPTCHA.
    """
    searxng_url = _searxng_url()
    if searxng_url:
        results = await search_searxng(searxng_url, query, max_results=max_results)
        if results:
            return results

    api_key = _tavily_api_key()
    if api_key:
        return await search_tavily(api_key, query, max_results=max_results)

    # Neither backend produced results. Distinguish "configured but empty" from
    # "nothing configured at all" so the caller (and the logs) can tell whether
    # the search genuinely failed or was never wired up. A silent ``[]`` here
    # is the most common cause of "the search ran but Synth got nothing".
    if searxng_url or api_key:
        log_warning(
            f"[web_search] No results for '{query}' from the configured "
            f"backend(s) (SearXNG={'yes' if searxng_url else 'no'}, "
            f"Tavily={'yes' if api_key else 'no'})."
        )
    else:
        log_error(
            f"[web_search] No web-search backend available for '{query}': "
            f"SearXNG is not configured/reachable AND no Tavily API key is set. "
            f"Set SEARXNG_URL or TAVILY_API_KEY, otherwise every search "
            f"returns empty."
        )
    return []


def _is_valid_result(hit: dict[str, str]) -> bool:
    """A result is *valid* only if it can actually be shown to the user.

    It must have a title and a URL, AND carry some usable text (a snippet or a
    fetched page). A result whose page was blocked by anti-bot protection or that
    returned no text is NOT valid: it must not count toward the requested number
    of results, otherwise the user would receive fewer usable results than asked.
    """
    if not str(hit.get("title", "") or "").strip():
        return False
    if not str(hit.get("url", "") or "").strip():
        return False
    snippet = str(hit.get("snippet", "") or "").strip()
    page = str(hit.get("page", "") or "").strip()
    if not snippet and not page:
        return False
    return True


async def collect_valid_results(
    query: str,
    min_valid: int = 5,
    max_candidates: int | None = None,
) -> list[dict[str, str]]:
    """Collect ``min_valid`` usable results for ``query``, paging past blocked ones.

    Unlike :func:`run_search` (which returns whatever the first page yields),
    this keeps pulling candidates — across pages and falling through from SearXNG
    to Tavily — until it has gathered ``min_valid`` *valid* results. A result is
    valid only if it has a title, a URL, and some usable text (see
    :func:`_is_valid_result`); results whose page was blocked by anti-bot
    protection or that returned no text do NOT count, so the user always gets the
    requested number of usable results.

    To guarantee termination, the search stops after ``max_candidates`` raw
    candidates have been examined (default ``3 * min_valid``). This bounds the
    work even when the backend keeps returning blocked/empty results, so the
    caller never loops forever. Returns fewer than ``min_valid`` results only
    when the backend is exhausted or unreachable.
    """
    if max_candidates is None:
        max_candidates = max(min_valid * 3, min_valid + 10)

    searxng_url = _searxng_url()
    api_key = _tavily_api_key()

    valid: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    candidates_examined = 0
    page = 1

    # Pull from SearXNG (paging) first, then fall through to Tavily.
    while len(valid) < min_valid and candidates_examined < max_candidates:
        batch: list[dict[str, str]] = []
        if searxng_url:
            batch = await search_searxng(
                searxng_url, query, max_results=max_candidates, page=page
            )
            page += 1
        elif api_key:
            # Tavily has no paging; one call with a generous limit suffices.
            batch = await search_tavily(api_key, query, max_results=max_candidates)

        if not batch:
            # SearXNG exhausted its pages (or no SearXNG) -> try Tavily once.
            if searxng_url and api_key:
                searxng_url = ""  # disable SearXNG, force Tavily on next loop
                continue
            break

        for hit in batch:
            if candidates_examined >= max_candidates:
                break
            candidates_examined += 1
            url = str(hit.get("url", "") or "").strip()
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            if _is_valid_result(hit):
                valid.append(hit)
                if len(valid) >= min_valid:
                    break

    log_info(
        f"[web_search] collect_valid_results('{query}'): {len(valid)} valid "
        f"of {min_valid} requested ({candidates_examined} candidates examined)"
    )
    return valid


class FetchCache:
    """Per-task shared cache of fetched page contents.

    Guarantees each URL is fetched at most once across all concurrent queries of a
    single search task. Concurrent callers for the same URL await one shared lock;
    the winner fetches, the others reuse the cached result.
    """

    def __init__(self) -> None:
        self._contents: dict[str, str] = {}
        self._detailed: dict[str, dict[str, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def has(self, url: str) -> bool:
        """Return True if ``url`` has already been fetched (result cached)."""
        return url in self._contents

    async def _lock_for(self, url: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(url)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[url] = lock
            return lock

    async def get_or_fetch(self, url: str, max_chars: int) -> str:
        """Return cached page text for ``url``, fetching it once if necessary."""
        if url in self._contents:
            return self._contents[url]

        lock = await self._lock_for(url)
        async with lock:
            # Re-check after acquiring the lock: a sibling query may have filled it.
            if url in self._contents:
                return self._contents[url]
            content = await _fetch_page_text(url, max_chars)
            self._contents[url] = content
            return content

    async def get_or_fetch_detailed(self, url: str, max_chars: int) -> dict[str, str]:
        """Return a cached structured fetch outcome, fetching once if necessary.

        Mirrors :meth:`get_or_fetch` but stores the full ``fetch_url_detailed``
        result (status/text/reason) so a URL passed both as a search result and
        as an explicit ``check_website`` link is scraped at most once per task.
        """
        if url in self._detailed:
            return self._detailed[url]

        lock = await self._lock_for(url)
        async with lock:
            if url in self._detailed:
                return self._detailed[url]
            outcome = await fetch_url_detailed(url, max_chars)
            self._detailed[url] = outcome
            return outcome


async def _fetch_page_text(url: str, max_chars: int) -> str:
    """Fetch a page and return cleaned, length-capped visible text."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return ""

    def _do_get() -> str:
        headers = {"User-Agent": _USER_AGENT}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.text

    try:
        html = await asyncio.to_thread(_do_get)
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = " ".join(text.split())
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "…"
        log_debug(f"[web_search] Fetched {len(text)} chars from {url}")
        return text
    except Exception as e:
        log_debug(f"[web_search] Page fetch failed for {url}: {e}")
        return ""


# HTTP status codes that indicate the request was actively refused, most often
# because the site detected automation (Cloudflare & friends commonly answer
# 403/429/503 to bot-like requests). These are treated as "blocked" so the
# caller can report the specific link as protected instead of silently empty.
# Language-agnostic on purpose: we key off HTTP status, never page text.
_ANTIBOT_STATUS_CODES = frozenset({401, 403, 405, 406, 429, 503})

# A successful (2xx) page that yields almost no extractable text is very likely
# a JS-rendered challenge/shell page (e.g. a Cloudflare "checking your browser"
# interstitial or an SPA that needs JavaScript). Without a headless browser we
# cannot render it, so we also surface these as "blocked".
_MIN_MEANINGFUL_TEXT_CHARS = 64


async def fetch_url_detailed(url: str, max_chars: int) -> dict[str, str]:
    """Fetch a single URL and return a structured outcome.

    Unlike :func:`_fetch_page_text` (which collapses every failure into an empty
    string), this returns *why* a fetch did not produce content so the caller can
    tell the user that a specific link is bot-protected rather than merely empty.

    Returns a dict ``{"url", "status", "text", "reason"}`` where ``status`` is one
    of:

    - ``"ok"``      — page fetched and meaningful text extracted (in ``text``);
    - ``"blocked"`` — the site refused the request or served an unrenderable
      challenge/JS shell (anti-bot); ``text`` is empty, ``reason`` explains it;
    - ``"invalid"`` — the URL is malformed / not http(s);
    - ``"error"``   — any other network/parse failure (``reason`` has details).

    NOTE: This uses a plain ``requests`` GET with no JavaScript rendering, so it
    cannot get past JS-based anti-bot challenges. In the future we could add an
    optional Playwright / headless-browser path here to render such pages before
    giving up and reporting the link as blocked.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return {
            "url": str(url or ""),
            "status": "invalid",
            "text": "",
            "reason": "not a valid http(s) URL",
        }

    def _do_get() -> tuple[int, str]:
        headers = {"User-Agent": _USER_AGENT}
        response = requests.get(url, headers=headers, timeout=10)
        return response.status_code, response.text

    try:
        status_code, html = await asyncio.to_thread(_do_get)
    except Exception as e:
        log_debug(f"[web_search] check_website network error for {url}: {e}")
        return {
            "url": url,
            "status": "error",
            "text": "",
            "reason": f"network error: {e}",
        }

    if status_code in _ANTIBOT_STATUS_CODES:
        log_debug(f"[web_search] check_website blocked (HTTP {status_code}) for {url}")
        return {
            "url": url,
            "status": "blocked",
            "text": "",
            "reason": f"anti-bot protection (HTTP {status_code})",
        }

    if status_code >= 400:
        return {
            "url": url,
            "status": "error",
            "text": "",
            "reason": f"HTTP {status_code}",
        }

    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = " ".join(text.split())
    except Exception as e:
        log_debug(f"[web_search] check_website parse error for {url}: {e}")
        return {
            "url": url,
            "status": "error",
            "text": "",
            "reason": f"parse error: {e}",
        }

    if len(text) < _MIN_MEANINGFUL_TEXT_CHARS:
        # 2xx but no real content: almost certainly a JS challenge or an
        # SPA shell that our non-rendering fetcher cannot read.
        log_debug(
            f"[web_search] check_website returned near-empty text ({len(text)} "
            f"chars) for {url}; treating as blocked"
        )
        return {
            "url": url,
            "status": "blocked",
            "text": "",
            "reason": "page requires JavaScript or is bot-protected "
            "(no readable content)",
        }

    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + "…"
    log_debug(f"[web_search] check_website fetched {len(text)} chars from {url}")
    return {"url": url, "status": "ok", "text": text, "reason": ""}
