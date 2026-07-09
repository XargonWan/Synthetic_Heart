"""Web search engines and page fetching with a per-task shared fetch cache.

This module owns the single implementation of the search backends (Tavily with a
keyless DuckDuckGo fallback via the ``ddgs`` package) and the page-content fetcher.
``web_search_plugin`` imports these helpers so there is exactly one implementation
of each.

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
    """Read the Tavily API key from the config registry (empty -> DuckDuckGo)."""
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
    """Search via the Tavily API. Falls back to DuckDuckGo on any failure."""

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
        log_warning(
            f"[web_search] Tavily API request failed: {e}. Falling back to DuckDuckGo..."
        )
        return await search_duckduckgo(query, max_results=max_results)


async def search_searxng(
    base_url: str, query: str, max_results: int = 5
) -> list[dict[str, str]]:
    """Search via a self-hosted SearXNG instance (JSON API).

    Queries ``{base_url}/search?format=json`` and maps the SearXNG result shape
    (``title`` / ``content`` / ``url``) to the plugin's ``{title, snippet, url}``
    shape. Returns an empty list on any failure so ``run_search`` can fall back to
    the next backend.
    """

    def _do_get() -> dict:
        headers = {"User-Agent": _USER_AGENT}
        params = {"q": query, "format": "json"}
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


async def search_duckduckgo(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search via DuckDuckGo (no API key required).

    Uses the ``ddgs`` package, which queries DuckDuckGo's JSON backend instead of
    scraping the HTML SERP. The old HTML scrape (``https://html.duckduckgo.com``)
    is now served a bot CAPTCHA and returns zero results, so it was replaced.
    """

    def _do_search() -> list[dict[str, str]]:
        from ddgs import DDGS

        results: list[dict[str, str]] = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                title = str(r.get("title") or "").strip()
                snippet = str(r.get("body") or "").strip()
                url = str(r.get("href") or "").strip()
                if title and url:
                    results.append({"title": title, "snippet": snippet, "url": url})
        return results

    try:
        results = await asyncio.to_thread(_do_search)
        log_info(f"[web_search] DuckDuckGo returned {len(results)} results")
        return results
    except Exception as e:
        log_error(f"[web_search] DuckDuckGo search failed: {e}")
        return []


async def run_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Run a single query on the active engine.

    Backend priority: SearXNG (self-hosted, if configured) -> Tavily (if keyed)
    -> DuckDuckGo (keyless fallback). Each backend that yields no results falls
    through to the next.
    """
    searxng_url = _searxng_url()
    if searxng_url:
        results = await search_searxng(searxng_url, query, max_results=max_results)
        if results:
            return results

    api_key = _tavily_api_key()
    if api_key:
        return await search_tavily(api_key, query, max_results=max_results)
    return await search_duckduckgo(query, max_results=max_results)


class FetchCache:
    """Per-task shared cache of fetched page contents.

    Guarantees each URL is fetched at most once across all concurrent queries of a
    single search task. Concurrent callers for the same URL await one shared lock;
    the winner fetches, the others reuse the cached result.
    """

    def __init__(self) -> None:
        self._contents: dict[str, str] = {}
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
