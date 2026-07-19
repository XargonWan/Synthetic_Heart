from __future__ import annotations

from typing import Any

import pytest

from plugins.recon_web_search import ReconWebSearchPlugin
from plugins.web_search import search_engine, search_orchestrator
from plugins.web_search.search_engine import FetchCache
from plugins.web_search.search_orchestrator import SearchOrchestrator


class _Msg:
    """Minimal message stub carrying text and an interface_path."""

    def __init__(self, text: str = "", interface_path: str = "test/iface") -> None:
        self.text = text
        self.interface_path = interface_path


@pytest.fixture
def enable_recon(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **_k: (
            True if key == "RECON_WEB_SEARCH_RECON_ENABLED" else default
        ),
    )


@pytest.mark.asyncio
async def test_recon_is_non_blocking_and_returns_instruction(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recon must only trigger the orchestrator and return an instruction."""
    submitted: dict[str, Any] = {}

    class _FakeOrchestrator:
        async def submit(
            self,
            *,
            interface_path: str | None,
            queries: list[str],
            search_context: str,
            context_memory: dict[str, Any] | None = None,
            urls: list[str] | None = None,
        ) -> str:
            submitted["interface_path"] = interface_path
            submitted["queries"] = queries
            submitted["search_context"] = search_context
            submitted["urls"] = urls
            return "task-xyz"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    msg = _Msg(text="what's the weather in Rome today?", interface_path="tg/42")
    out = await plugin.parse_recon_response(
        {"web_search": ["weather Rome today", "Rome forecast"]},
        message=msg,
        context_memory={"foo": "bar"},
        text="what's the weather in Rome today?",
    )

    assert submitted["interface_path"] == "tg/42"
    assert submitted["queries"] == ["weather Rome today", "Rome forecast"]
    assert "what's the weather in Rome today?" in submitted["search_context"]
    assert len(out) == 1
    assert out[0]["type"] == "instruction"
    assert out[0]["source"] == "recon_web_search"


@pytest.mark.asyncio
async def test_recon_no_queries_emits_guard_and_does_not_submit(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"submit": False}

    class _FakeOrchestrator:
        async def submit(self, **_k: Any) -> str:
            called["submit"] = True
            return "x"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    out = await plugin.parse_recon_response(
        {"web_search": []}, message=_Msg(), context_memory={}
    )
    # No search was triggered, so no task is submitted...
    assert called["submit"] is False
    # ...but a guard instruction is returned so the persona does not falsely
    # announce a search that is not running.
    assert len(out) == 1
    assert out[0]["type"] == "instruction"
    assert out[0]["source"] == "recon_web_search"


@pytest.mark.asyncio
async def test_recon_recovers_explicit_url_when_llm_omits_it(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link pasted by the user must trigger a fetch even if the recon LLM
    returns an empty check_website (weak-model backstop).

    Reproduces the incident where the recon model emitted
    {"queries": [], "check_website": []} for a message that contained an
    explicit URL, so no search fired and the promised follow-up never arrived.
    """
    submitted: dict[str, Any] = {}

    class _FakeOrchestrator:
        async def submit(
            self,
            *,
            interface_path: str | None,
            queries: list[str],
            search_context: str,
            context_memory: dict[str, Any] | None = None,
            urls: list[str] | None = None,
        ) -> str:
            submitted["queries"] = queries
            submitted["urls"] = urls
            return "task-recovered"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    msg = _Msg(
        text="Guarda qua: https://example.com/article Chissà cosa dice.",
        interface_path="tg/7",
    )
    out = await plugin.parse_recon_response(
        {"web_search": {"queries": [], "check_website": []}},
        message=msg,
        context_memory={},
        text="Guarda qua: https://example.com/article Chissà cosa dice.",
    )

    # The explicit URL from the message was recovered and submitted.
    assert submitted["urls"] == ["https://example.com/article"]
    assert submitted["queries"] == []
    # A follow-up instruction is emitted (a search IS now running).
    assert len(out) == 1
    assert out[0]["type"] == "instruction"
    assert out[0]["source"] == "recon_web_search"


@pytest.mark.asyncio
async def test_recon_no_url_in_message_still_emits_guard(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When neither the LLM nor the message provides a link/query, the guard
    instruction must still be returned and no task submitted."""
    called = {"submit": False}

    class _FakeOrchestrator:
        async def submit(self, **_k: Any) -> str:
            called["submit"] = True
            return "x"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    out = await plugin.parse_recon_response(
        {"web_search": {"queries": [], "check_website": []}},
        message=_Msg(text="just a plain question with no link"),
        context_memory={},
        text="just a plain question with no link",
    )
    assert called["submit"] is False
    assert len(out) == 1
    assert out[0]["source"] == "recon_web_search"


@pytest.mark.asyncio
async def test_recon_skips_result_delivery_beat_and_does_not_resubmit(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second turn delivering results must not trigger a new search.

    The web_search_result beat's prompt still mentions a search, so without a
    guard the recon LLM would generate fresh queries and this plugin would spawn
    another background task — an infinite loop of "searching..." announcements.
    """
    called = {"submit": False}

    class _FakeOrchestrator:
        async def submit(self, **_k: Any) -> str:
            called["submit"] = True
            return "x"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    out = await plugin.parse_recon_response(
        {"web_search": ["some query"]},
        message=_Msg(),
        context_memory={"beat_type": "web_search_result"},
    )
    # No new search is submitted for the delivery turn...
    assert called["submit"] is False
    # ...and no instruction is emitted (results are already in the prompt).
    assert out == []


@pytest.mark.asyncio
async def test_fetch_cache_dedups_across_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL scraped by one query must not be re-fetched by a sibling query."""
    fetch_calls: list[str] = []

    async def _fake_fetch(url: str, max_chars: int) -> str:
        fetch_calls.append(url)
        return f"page-of-{url}"

    monkeypatch.setattr(search_engine, "_fetch_page_text", _fake_fetch)

    cache = FetchCache()
    a = await cache.get_or_fetch("http://x.test/a", 100)
    b = await cache.get_or_fetch("http://x.test/a", 100)
    c = await cache.get_or_fetch("http://x.test/b", 100)

    assert a == b == "page-of-http://x.test/a"
    assert c == "page-of-http://x.test/b"
    assert fetch_calls == ["http://x.test/a", "http://x.test/b"]


@pytest.mark.asyncio
async def test_orchestrator_delivers_second_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished task must enqueue a second turn on the originating path."""
    statuses: list[str] = []

    async def _fake_init() -> None:
        return None

    async def _fake_insert(*_a: Any, **_k: Any) -> None:
        return None

    async def _fake_update(task_id: str, status: str, **_k: Any) -> None:
        statuses.append(status)

    monkeypatch.setattr(search_orchestrator, "_init_table", _fake_init)
    monkeypatch.setattr(search_orchestrator, "_insert_task", _fake_insert)
    monkeypatch.setattr(search_orchestrator, "_update_status", _fake_update)

    orch = SearchOrchestrator()

    async def _fake_search(
        self: SearchOrchestrator,
        queries: list[str],
        urls: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        return [{"query": queries[0], "results": []}], []

    async def _fake_synth(
        self: SearchOrchestrator,
        blocks: list[dict[str, Any]],
        search_context: str,
        link_outcomes: list[dict[str, str]] | None = None,
    ) -> str:
        return "SYNTHESIZED RESULT"

    monkeypatch.setattr(SearchOrchestrator, "_search", _fake_search)
    monkeypatch.setattr(SearchOrchestrator, "_synthesize", _fake_synth)

    enqueued: dict[str, Any] = {}

    class _FakeQueue:
        @staticmethod
        async def enqueue_low_priority(
            bot: Any,
            message: Any,
            *,
            context_memory: dict[str, Any] | None = None,
            interface_id: str | None = None,
            original_message: Any = None,
        ) -> None:
            enqueued["interface_path"] = getattr(message, "interface_path", None)
            enqueued["context"] = context_memory
            enqueued["interface_id"] = interface_id

    import core

    monkeypatch.setattr(core, "message_queue", _FakeQueue, raising=False)

    await orch._run_task(
        task_id="t1",
        interface_path="discord/guild/chan",
        queries=["q1"],
        search_context="the intent",
        context_memory={"k": "v"},
    )

    assert "running" in statuses
    assert "done" in statuses
    assert enqueued["interface_path"] == "discord/guild/chan"
    assert enqueued["interface_id"] == "web_search"
    ctx = enqueued["context"]
    assert ctx["beat_type"] == "web_search_result"
    assert ctx["grillo_beat"] is True
    assert ctx["web_search_task_id"] == "t1"
    assert ctx["prior_context"] == {"k": "v"}


def test_web_search_result_is_outbound_beat() -> None:
    from core.beat_utils import is_outbound_beat

    assert is_outbound_beat("web_search_result") is True


@pytest.mark.asyncio
async def test_run_search_uses_searxng_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SearXNG is configured, run_search routes to the SearXNG backend."""
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "http://searxng:8888")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    called: dict[str, Any] = {}

    async def _fake_searxng(
        base_url: str, query: str, max_results: int = 5
    ) -> list[dict[str, str]]:
        called["base_url"] = base_url
        called["query"] = query
        called["max_results"] = max_results
        return [{"title": "T", "snippet": "S", "url": "https://example.com"}]

    monkeypatch.setattr(search_engine, "search_searxng", _fake_searxng)

    results = await search_engine.run_search("hello", max_results=3)

    assert called == {
        "base_url": "http://searxng:8888",
        "query": "hello",
        "max_results": 3,
    }
    assert results == [{"title": "T", "snippet": "S", "url": "https://example.com"}]


@pytest.mark.asyncio
async def test_run_search_falls_back_to_tavily_when_searxng_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SearXNG returning nothing falls through to Tavily when a key is set."""
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "http://searxng:8888")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "tvly-key")

    async def _empty_searxng(
        base_url: str, query: str, max_results: int = 5
    ) -> list[dict[str, str]]:
        return []

    called: dict[str, Any] = {}

    async def _fake_tavily(
        api_key: str, query: str, max_results: int = 5
    ) -> list[dict[str, str]]:
        called["api_key"] = api_key
        called["query"] = query
        return [{"title": "T", "snippet": "S", "url": "https://example.com"}]

    monkeypatch.setattr(search_engine, "search_searxng", _empty_searxng)
    monkeypatch.setattr(search_engine, "search_tavily", _fake_tavily)

    results = await search_engine.run_search("hello", max_results=3)

    assert called == {"api_key": "tvly-key", "query": "hello"}
    assert results == [{"title": "T", "snippet": "S", "url": "https://example.com"}]


@pytest.mark.asyncio
async def test_run_search_returns_empty_when_no_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With neither SearXNG nor a Tavily key configured, run_search returns []."""
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    results = await search_engine.run_search("hello", max_results=3)

    assert results == []


@pytest.mark.asyncio
async def test_collect_valid_results_skips_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked/empty results must NOT count toward the requested number.

    Reproduces the user's note: the first page may return 5 hits but some are
    anti-bot-blocked (no usable text), so the collector must page past them and
    keep going until it has gathered ``min_valid`` *valid* results.
    """
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "http://sx")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    # Page 1: 4 blocked (no snippet/page) + 1 valid. Page 2: 5 valid.
    valid = lambda i: {"title": f"v{i}", "snippet": "ok", "url": f"https://v{i}.x"}
    blocked = lambda i: {"title": f"b{i}", "snippet": "", "url": f"https://b{i}.x"}

    pages = [
        [blocked(0), blocked(1), blocked(2), blocked(3), valid(0)],
        [valid(1), valid(2), valid(3), valid(4), valid(5)],
    ]

    async def _fake_searxng(base_url, query, max_results=5, page=1):
        return pages[page - 1][:max_results]

    monkeypatch.setattr(search_engine, "search_searxng", _fake_searxng)

    out = await search_engine.collect_valid_results("q", min_valid=5)
    assert len(out) == 5
    # The blocked hits must not appear in the final set.
    assert all("b" not in r["title"] for r in out)
    assert out[0]["title"] == "v0"


@pytest.mark.asyncio
async def test_collect_valid_results_caps_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the backend only returns blocked results, the collector must stop.

    Guards against an infinite loop: even if every candidate is invalid, the search
    terminates once ``max_candidates`` have been examined and returns whatever
    (possibly fewer than ``min_valid``) valid results it found.
    """
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "http://sx")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    blocked = {"title": "b", "snippet": "", "url": "https://b.x"}

    async def _fake_searxng(base_url, query, max_results=5, page=1):
        # Always returns blocked hits, never exhausts — would loop forever without a cap.
        return [blocked for _ in range(max_results)]

    monkeypatch.setattr(search_engine, "search_searxng", _fake_searxng)

    out = await search_engine.collect_valid_results("q", min_valid=5, max_candidates=9)
    assert out == []  # nothing valid, but it returned instead of hanging


@pytest.mark.asyncio
async def test_recon_extracts_check_website_from_object_form(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The object form must split into queries and check_website URLs."""
    submitted: dict[str, Any] = {}

    class _FakeOrchestrator:
        async def submit(
            self,
            *,
            interface_path: str | None,
            queries: list[str],
            search_context: str,
            context_memory: dict[str, Any] | None = None,
            urls: list[str] | None = None,
        ) -> str:
            submitted["queries"] = queries
            submitted["urls"] = urls
            return "task-xyz"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    # recon.py passes the VALUE under the "web_search" key, not the wrapper.
    out = await plugin.parse_recon_response(
        {
            "queries": ["latest news"],
            "check_website": ["https://example.com/page"],
        },
        message=_Msg(interface_path="tg/7"),
        context_memory={},
        text="check https://example.com/page and search latest news",
    )

    assert submitted["queries"] == ["latest news"]
    assert submitted["urls"] == ["https://example.com/page"]
    assert len(out) == 1
    assert out[0]["type"] == "instruction"


@pytest.mark.asyncio
async def test_recon_extracts_check_website_from_double_wrapped_form(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weak recon model may echo the wrapper key; unwrap it defensively."""
    submitted: dict[str, Any] = {}

    class _FakeOrchestrator:
        async def submit(
            self,
            *,
            interface_path: str | None,
            queries: list[str],
            search_context: str,
            context_memory: dict[str, Any] | None = None,
            urls: list[str] | None = None,
        ) -> str:
            submitted["queries"] = queries
            submitted["urls"] = urls
            return "task-xyz"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    # Some recon models emit {"web_search": {...}} as the value under the
    # already-namespaced "web_search" key, producing a redundant nesting.
    out = await plugin.parse_recon_response(
        {
            "web_search": {
                "queries": [],
                "check_website": [
                    "https://synthetic-heart.readthedocs.io/en/latest/"
                    "chat_instructions.html"
                ],
            }
        },
        message=_Msg(interface_path="tg/7"),
        context_memory={},
        text="Rekku, can you see this site? https://synthetic-heart.readthedocs.io",
    )

    assert submitted["queries"] == []
    assert submitted["urls"] == [
        "https://synthetic-heart.readthedocs.io/en/latest/chat_instructions.html"
    ]
    assert len(out) == 1
    assert out[0]["type"] == "instruction"


@pytest.mark.asyncio
async def test_recon_check_website_only_triggers_without_queries(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link with no query must still trigger the background task."""
    submitted: dict[str, Any] = {}

    class _FakeOrchestrator:
        async def submit(
            self,
            *,
            interface_path: str | None,
            queries: list[str],
            search_context: str,
            context_memory: dict[str, Any] | None = None,
            urls: list[str] | None = None,
        ) -> str:
            submitted["queries"] = queries
            submitted["urls"] = urls
            return "task-xyz"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    # recon.py passes the VALUE under the "web_search" key, not the wrapper.
    out = await plugin.parse_recon_response(
        {"queries": [], "check_website": ["https://a.test/x"]},
        message=_Msg(interface_path="tg/7"),
        context_memory={},
        text="look at https://a.test/x",
    )

    assert submitted["queries"] == []
    assert submitted["urls"] == ["https://a.test/x"]
    assert len(out) == 1
    assert out[0]["type"] == "instruction"


@pytest.mark.asyncio
async def test_search_partial_link_failure_does_not_fail_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreachable URL among several must not fail the whole search."""

    async def _fake_detailed(
        self: FetchCache, url: str, max_chars: int
    ) -> dict[str, str]:
        if "blocked" in url:
            return {"url": url, "status": "blocked", "text": "", "reason": "anti-bot"}
        return {"url": url, "status": "ok", "text": f"content-{url}", "reason": ""}

    monkeypatch.setattr(FetchCache, "get_or_fetch_detailed", _fake_detailed)

    orch = SearchOrchestrator()
    blocks, link_outcomes = await orch._search(
        [], urls=["https://ok.test/a", "https://blocked.test/b"]
    )

    assert blocks == []
    statuses = {o["url"]: o["status"] for o in link_outcomes}
    assert statuses == {
        "https://ok.test/a": "ok",
        "https://blocked.test/b": "blocked",
    }
