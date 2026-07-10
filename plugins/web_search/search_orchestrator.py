"""Background web-search orchestrator.

Runs searches OUTSIDE the normal Synth message lifecycle. Recon only *triggers*
this orchestrator (fire-and-forget); the heavy work happens here:

1. All queries of a task run concurrently, sharing one :class:`FetchCache` so a
   URL scraped by one query is never re-scraped by a sibling query.
2. The raw, aggregated results are handed to the cortex (GRILLO scope) as an
   *aseptic* text processor — no persona, no tools — to fuse them into a single
   factual summary with source URLs.
3. Synth is woken with a second turn via an outbound low-priority beat on the
   originating ``interface_path``, carrying the synthesized text, the context at
   search time, and the beat metadata.

Task state is persisted in the ``web_search_tasks`` table (pending -> running ->
done/error). In-flight tasks are lost on restart and left as ``running`` rows;
they are not resumed (out of scope).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning

from .search_engine import FetchCache, run_search

# Expose the orchestrator's tunables in the WebUI / env loader. Registration is
# best-effort so importing the orchestrator never fails if the variables engine
# is unavailable (e.g. in isolated unit tests).
try:
    from core.variables_engine import register_exposed_var

    _EXPOSED_VARS: list[tuple[str, str, object, type, str]] = [
        (
            "WEB_SEARCH_MAX_QUERIES",
            "Web Search Max Queries",
            3,
            int,
            "Maximum number of search queries executed per background search task.",
        ),
        (
            "WEB_SEARCH_RESULTS_PER_QUERY",
            "Web Search Results Per Query",
            5,
            int,
            "Number of search-engine results collected for each query.",
        ),
        (
            "WEB_SEARCH_FETCH_PAGES",
            "Web Search Fetch Pages",
            True,
            bool,
            "If enabled, fetch and scrape the top result pages for richer context.",
        ),
        (
            "WEB_SEARCH_FETCH_TOP_N",
            "Web Search Fetch Top N",
            3,
            int,
            "How many top result pages to fetch per query when page fetching is on.",
        ),
        (
            "WEB_SEARCH_PAGE_MAX_CHARS",
            "Web Search Page Max Chars",
            4000,
            int,
            "Maximum characters extracted from each fetched page.",
        ),
        (
            "WEB_SEARCH_FETCH_TIMEOUT",
            "Web Search Fetch Timeout (s)",
            120,
            int,
            "Timeout in seconds for the SEARCH phase only (querying the search "
            "backend and scraping result pages). This bounds network I/O and does "
            "NOT include the synthesis LLM call, which is left untimed so that "
            "time spent waiting in a serial engine's queue (e.g. "
            "selenium-llm-engine, one shared Chromium worker) never cancels the "
            "task — such queueing can last minutes or hours under load.",
        ),
    ]
    for _name, _label, _default, _vtype, _desc in _EXPOSED_VARS:
        register_exposed_var(
            _name,
            label=_label,
            default=_default,
            value_type=_vtype,
            ui_type="bool" if _vtype is bool else "int",
            description=_desc,
            scope="agent",
            component="agent",
        )
except Exception:
    pass

# Keep strong references to fire-and-forget tasks so they are not garbage
# collected mid-flight (see AGENTS.md RUF006 note).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config_registry.get_value(key, default))
    except Exception:
        return default


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        val = config_registry.get_value(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


async def _init_table() -> None:
    """Create the ``web_search_tasks`` table if it does not exist (backend-aware)."""
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS web_search_tasks (
                    id VARCHAR(64) PRIMARY KEY,
                    interface_path TEXT,
                    queries TEXT,
                    search_context TEXT,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    result_text LONGTEXT,
                    error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        await conn.commit()


async def _insert_task(
    task_id: str,
    interface_path: str | None,
    queries: list[str],
    search_context: str,
    urls: list[str] | None = None,
) -> None:
    from core.db import get_conn_ctx

    # The schema has a single `queries` TEXT column. To persist the optional
    # direct-visit URLs without a migration, we store a JSON object
    # ``{"queries": [...], "urls": [...]}`` whenever URLs are present, and keep
    # the legacy bare-list JSON when there are none (backward compatible).
    if urls:
        queries_blob = json.dumps(
            {"queries": queries, "urls": urls}, ensure_ascii=False
        )
    else:
        queries_blob = json.dumps(queries, ensure_ascii=False)

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO web_search_tasks
                    (id, interface_path, queries, search_context, status)
                VALUES (%s, %s, %s, %s, 'pending')
                """,
                (
                    task_id,
                    interface_path or "",
                    queries_blob,
                    search_context or "",
                ),
            )
        await conn.commit()


async def _update_status(
    task_id: str,
    status: str,
    *,
    result_text: str | None = None,
    error: str | None = None,
) -> None:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE web_search_tasks
                SET status = %s, result_text = %s, error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, result_text, error, task_id),
            )
        await conn.commit()


class SearchOrchestrator:
    """Singleton owner of background web-search tasks."""

    def __init__(self) -> None:
        self._table_ready = False

    async def _ensure_table(self) -> None:
        if self._table_ready:
            return
        try:
            await _init_table()
            self._table_ready = True
        except Exception as e:
            log_error(f"[web_search] Could not init web_search_tasks table: {e}")

    async def submit(
        self,
        *,
        interface_path: str | None,
        queries: list[str],
        search_context: str,
        context_memory: dict[str, Any] | None = None,
        urls: list[str] | None = None,
    ) -> str:
        """Register a task and launch it in the background. Returns immediately.

        This is the entry point called by recon. It NEVER blocks on the actual
        search — it only records the task and schedules the background coroutine.

        ``queries`` are internet searches (SearXNG/Tavily); ``urls`` are explicit
        links the user asked Synth to visit directly (``check_website``). Both are
        handled by the SAME task and delivered together in a single second turn.
        """
        max_queries = _cfg_int("WEB_SEARCH_MAX_QUERIES", 3)
        clean = [q.strip() for q in queries if q and q.strip()][:max_queries]

        max_urls = _cfg_int("WEB_SEARCH_MAX_QUERIES", 3)
        clean_urls = [
            u.strip()
            for u in (urls or [])
            if u and u.strip().lower().startswith(("http://", "https://"))
        ][:max_urls]

        task_id = uuid.uuid4().hex

        await self._ensure_table()
        try:
            await _insert_task(
                task_id, interface_path, clean, search_context, urls=clean_urls
            )
        except Exception as e:
            log_error(f"[web_search] Failed to persist task {task_id}: {e}")

        task = asyncio.create_task(
            self._run_task(
                task_id=task_id,
                interface_path=interface_path,
                queries=clean,
                search_context=search_context,
                context_memory=dict(context_memory or {}),
                urls=clean_urls,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        log_info(
            f"[web_search] Submitted task {task_id} "
            f"({len(clean)} queries, {len(clean_urls)} direct link(s)) "
            f"for path={interface_path}"
        )
        return task_id

    async def _run_task(
        self,
        *,
        task_id: str,
        interface_path: str | None,
        queries: list[str],
        search_context: str,
        context_memory: dict[str, Any],
        urls: list[str] | None = None,
    ) -> None:
        fetch_timeout = _cfg_int("WEB_SEARCH_FETCH_TIMEOUT", 120)
        urls = urls or []
        try:
            await _update_status(task_id, "running")
            # The SEARCH phase (network I/O) is bounded by a timeout. The
            # SYNTHESIS phase is intentionally left untimed here: it may sit in a
            # serial engine's queue for a very long time before the worker even
            # picks it up, and cancelling on wall-clock time would abort a task
            # that is merely waiting its turn — the engine owns its own
            # per-request timeout once it starts processing.
            blocks, link_outcomes = await asyncio.wait_for(
                self._search(queries, urls),
                timeout=fetch_timeout,
            )
            result_text = await self._synthesize(blocks, search_context, link_outcomes)
            await _update_status(task_id, "done", result_text=result_text)
            await self._deliver(
                interface_path=interface_path,
                result_text=result_text,
                search_context=search_context,
                queries=queries,
                context_memory=context_memory,
                task_id=task_id,
                urls=urls,
                link_outcomes=link_outcomes,
            )
        except asyncio.TimeoutError:
            log_warning(
                f"[web_search] Task {task_id} search phase timed out after "
                f"{fetch_timeout}s"
            )
            await _update_status(task_id, "error", error="search_timeout")
        except Exception as e:
            log_error(f"[web_search] Task {task_id} failed: {e}")
            await _update_status(task_id, "error", error=str(e))

    async def _search(
        self, queries: list[str], urls: list[str] | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Run queries and direct-visit URLs concurrently with a shared cache.

        Returns ``(blocks, link_outcomes)`` where ``blocks`` are the per-query
        search results and ``link_outcomes`` are the structured per-URL results
        of the explicit ``check_website`` visits (each a dict from
        ``fetch_url_detailed``). A failed link never fails the task: it is simply
        reported with its ``blocked``/``error`` status.
        """
        results_per_query = _cfg_int("WEB_SEARCH_RESULTS_PER_QUERY", 5)
        fetch_pages = _cfg_bool("WEB_SEARCH_FETCH_PAGES", True)
        fetch_top_n = _cfg_int("WEB_SEARCH_FETCH_TOP_N", 3)
        page_max_chars = _cfg_int("WEB_SEARCH_PAGE_MAX_CHARS", 4000)
        urls = urls or []

        cache = FetchCache()

        async def _one_query(query: str) -> dict[str, Any]:
            hits = await run_search(query, max_results=results_per_query)
            enriched: list[dict[str, str]] = []
            for idx, hit in enumerate(hits):
                page = ""
                url = hit.get("url", "")
                if fetch_pages and url and idx < fetch_top_n:
                    page = await cache.get_or_fetch(url, page_max_chars)
                enriched.append(
                    {
                        "title": hit.get("title", ""),
                        "snippet": hit.get("snippet", ""),
                        "url": url,
                        "page": page,
                    }
                )
            return {"query": query, "results": enriched}

        async def _one_url(url: str) -> dict[str, str]:
            return await cache.get_or_fetch_detailed(url, page_max_chars)

        query_tasks = [_one_query(q) for q in queries]
        url_tasks = [_one_url(u) for u in urls]
        gathered = await asyncio.gather(
            *query_tasks, *url_tasks, return_exceptions=True
        )

        n_queries = len(query_tasks)
        blocks: list[dict[str, Any]] = []
        for item in gathered[:n_queries]:
            if isinstance(item, dict):
                blocks.append(item)
            else:
                log_debug(f"[web_search] query error: {item}")

        link_outcomes: list[dict[str, str]] = []
        for url, item in zip(urls, gathered[n_queries:]):
            if isinstance(item, dict):
                link_outcomes.append(item)
            else:
                # An unexpected exception fetching this specific link: report it
                # as an error outcome instead of failing the whole task.
                log_debug(f"[web_search] check_website error for {url}: {item}")
                link_outcomes.append(
                    {
                        "url": url,
                        "status": "error",
                        "text": "",
                        "reason": f"unexpected error: {item}",
                    }
                )

        return blocks, link_outcomes

    async def _synthesize(
        self,
        blocks: list[dict[str, Any]],
        search_context: str,
        link_outcomes: list[dict[str, str]] | None = None,
    ) -> str:
        """Fuse raw results into a single aseptic factual text via the cortex."""
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry

        link_outcomes = link_outcomes or []

        payload_blocks = []
        for b in blocks:
            payload_blocks.append(
                {
                    "query": b.get("query", ""),
                    "results": [
                        {
                            "title": r.get("title", ""),
                            "snippet": r.get("snippet", ""),
                            "url": r.get("url", ""),
                            "page": r.get("page", ""),
                        }
                        for r in b.get("results", [])
                    ],
                }
            )

        # Direct-visit links: successful ones contribute their page text; failed
        # ones are passed as structured "not visitable" notes so the synthesis
        # can tell the user exactly which links could not be read and why.
        visited_links = [
            {"url": o.get("url", ""), "page": o.get("text", "")}
            for o in link_outcomes
            if o.get("status") == "ok" and o.get("text")
        ]
        unreachable_links = [
            {
                "url": o.get("url", ""),
                "status": o.get("status", "error"),
                "reason": o.get("reason", ""),
            }
            for o in link_outcomes
            if o.get("status") != "ok"
        ]

        instructions = (
            "You are an ASEPTIC text processor, NOT a persona. Merge the raw web "
            "search results and the directly-visited link contents below into a "
            "SINGLE factual, well-structured text that answers the search intent. "
            "Rules: report only facts present in the results; do NOT invent "
            "anything; cite the source URL after each fact or claim; no first "
            "person, no opinions, no persona voice; write in the same language as "
            "the search intent. If any 'unreachable_links' are listed, state "
            "plainly that those specific links could not be visited (and why), "
            "without failing the rest. If the results are insufficient, say so "
            "plainly."
        )
        prompt = {
            "input": {
                "type": "web_search_synthesis",
                "payload": {
                    "search_intent": search_context,
                    "blocks": payload_blocks,
                    "visited_links": visited_links,
                    "unreachable_links": unreachable_links,
                },
            },
            "context": {},
            "instructions": instructions,
        }

        total_results = sum(len(b.get("results", [])) for b in payload_blocks)
        total_chars = sum(
            len(r.get("title", "")) + len(r.get("snippet", "")) + len(r.get("page", ""))
            for b in payload_blocks
            for r in b.get("results", [])
        )
        log_info(
            f"[web_search] Synthesizing {total_results} result(s) across "
            f"{len(payload_blocks)} query block(s) ({total_chars} chars), "
            f"{len(visited_links)} visited link(s), "
            f"{len(unreachable_links)} unreachable link(s) "
            f"for intent={search_context!r}"
        )

        # Always build a raw factual digest from the actual results. This is the
        # ground truth: even if the synthesis cortex ignores or hallucinates over
        # the payload, Synth must still receive the real facts + sources so she
        # never reports "no results were passed to me" when there clearly were.
        raw_digest = self._raw_results_digest(blocks, link_outcomes)

        # If we genuinely have no search results AND no link outcomes at all,
        # there is nothing to synthesize or report.
        if total_results == 0 and not link_outcomes:
            log_info(
                f"[web_search] No results to synthesize for intent={search_context!r}"
            )
            return ""

        synthesized = ""
        try:
            active_cortex = await get_active_cortex_engine(scope="grillo")
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex)
            if engine is None:
                engine = registry.load_engine(active_cortex)
            log_info(
                f"[web_search] Synthesis using cortex engine={active_cortex!r} "
                f"(scope=grillo)"
            )
            resp = await engine.generate_response(prompt)
            synthesized = (resp if isinstance(resp, str) else str(resp)).strip()
            log_info(
                f"[web_search] Synthesis produced {len(synthesized)} chars for "
                f"intent={search_context!r}"
            )
        except Exception as e:
            log_error(f"[web_search] Synthesis failed: {e}")
            synthesized = ""

        # Guard against a synthesis engine that ignored the payload and returned
        # nothing (or something too short to carry the facts). In that case the
        # raw digest alone is the deliverable.
        if len(synthesized) < 40:
            log_info(
                "[web_search] Synthesis empty/too short; delivering raw results "
                "digest only"
            )
            return raw_digest

        # Attach the raw sources beneath the synthesized narrative so Synth always
        # has the verifiable facts, regardless of how the synthesis model behaved.
        return f"{synthesized}\n\n--- RAW SOURCES ---\n{raw_digest}".strip()

    @staticmethod
    def _raw_results_digest(
        blocks: list[dict[str, Any]],
        link_outcomes: list[dict[str, str]] | None = None,
    ) -> str:
        """Build a plain, source-cited digest directly from the search results."""
        lines: list[str] = []
        for b in blocks:
            query = b.get("query", "")
            results = b.get("results", [])
            if query:
                lines.append(f"# {query}")
            for r in results:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                url = r.get("url", "")
                lines.append(f"- {title}: {snippet} ({url})")

        for outcome in link_outcomes or []:
            url = outcome.get("url", "")
            status = outcome.get("status", "")
            if status == "ok":
                text = outcome.get("text", "")
                lines.append(f"# Visited link: {url}")
                if text:
                    lines.append(text)
            else:
                reason = outcome.get("reason", "") or status
                lines.append(f"# Unreachable link: {url} ({reason})")

        return "\n".join(lines).strip()

    async def _deliver(
        self,
        *,
        interface_path: str | None,
        result_text: str,
        search_context: str,
        queries: list[str],
        context_memory: dict[str, Any],
        task_id: str,
        urls: list[str] | None = None,
        link_outcomes: list[dict[str, str]] | None = None,
    ) -> None:
        """Wake Synth with a second turn carrying the synthesized results."""
        if not result_text:
            log_info(f"[web_search] Task {task_id}: empty result, skipping delivery")
            return

        urls = urls or []
        link_outcomes = link_outcomes or []

        try:
            from core import message_queue

            links_line = f"Direct links: {', '.join(urls)}\n" if urls else ""
            prompt = (
                "=== WEB SEARCH RESULTS ===\n"
                "A background web search you announced earlier has completed. "
                "Report the findings to the user naturally, in your own voice, in "
                "their language. The following is an aseptic factual summary with "
                "sources — do not read it verbatim, integrate it. If any links "
                "could not be visited, tell the user which specific ones failed "
                "(and why) while still reporting everything that succeeded.\n\n"
                f"Search intent: {search_context}\n"
                f"Queries: {', '.join(queries)}\n"
                f"{links_line}"
                "\n"
                f"{result_text}"
            )

            message = SimpleNamespace()
            message.chat_id = -1
            message.message_id = 0
            message.text = prompt
            message.from_user = SimpleNamespace(
                id=-1, username="web_search", full_name="Web Search"
            )
            message.chat = SimpleNamespace(id=-1, type="internal")
            message.date = datetime.now(timezone.utc)
            if interface_path:
                message.interface_path = interface_path

            context = {
                "grillo_beat": True,
                "beat_type": "web_search_result",
                "interface_path": interface_path,
                "web_search_task_id": task_id,
                "web_search_context": search_context,
                "web_search_queries": queries,
                "web_search_urls": urls,
                "web_search_link_outcomes": link_outcomes,
                "prior_context": context_memory,
            }

            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=context,
                interface_id="web_search",
                original_message=None,
            )
            log_info(
                f"[web_search] Task {task_id}: delivered second turn on "
                f"path={interface_path}"
            )
        except Exception as e:
            log_error(f"[web_search] Task {task_id}: delivery failed: {e}")


_ORCHESTRATOR: SearchOrchestrator | None = None


def get_search_orchestrator() -> SearchOrchestrator:
    """Return the process-wide search orchestrator singleton."""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        _ORCHESTRATOR = SearchOrchestrator()
    return _ORCHESTRATOR
