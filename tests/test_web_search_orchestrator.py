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
        ) -> str:
            submitted["interface_path"] = interface_path
            submitted["queries"] = queries
            submitted["search_context"] = search_context
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
        self: SearchOrchestrator, queries: list[str]
    ) -> list[dict[str, Any]]:
        return [{"query": queries[0], "results": []}]

    async def _fake_synth(
        self: SearchOrchestrator, blocks: list[dict[str, Any]], search_context: str
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


class _FakeDDGS:
    """Stub for ddgs.DDGS as a context manager yielding fixed text results."""

    _RESULTS: list[dict[str, str]] = []

    def __enter__(self) -> _FakeDDGS:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def text(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        return list(self._RESULTS[:max_results])


@pytest.mark.asyncio
async def test_search_duckduckgo_uses_ddgs(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_duckduckgo maps ddgs text() rows to title/snippet/url dicts."""
    import ddgs as ddgs_module

    _FakeDDGS._RESULTS = [
        {"title": "T1", "body": "B1", "href": "https://example.com/1"},
        {"title": "T2", "body": "B2", "href": "https://example.com/2"},
    ]
    monkeypatch.setattr(ddgs_module, "DDGS", _FakeDDGS)

    results = await search_engine.search_duckduckgo("some query", max_results=5)

    assert results == [
        {"title": "T1", "snippet": "B1", "url": "https://example.com/1"},
        {"title": "T2", "snippet": "B2", "url": "https://example.com/2"},
    ]


@pytest.mark.asyncio
async def test_search_duckduckgo_skips_rows_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows missing a title or url are dropped."""
    import ddgs as ddgs_module

    _FakeDDGS._RESULTS = [
        {"title": "Good", "body": "b", "href": "https://example.com/ok"},
        {"title": "", "body": "b", "href": "https://example.com/no-title"},
        {"title": "No URL", "body": "b", "href": ""},
    ]
    monkeypatch.setattr(ddgs_module, "DDGS", _FakeDDGS)

    results = await search_engine.search_duckduckgo("q", max_results=5)

    assert results == [
        {"title": "Good", "snippet": "b", "url": "https://example.com/ok"},
    ]


@pytest.mark.asyncio
async def test_search_duckduckgo_returns_empty_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside ddgs yields an empty list, never an exception."""
    import ddgs as ddgs_module

    class _BoomDDGS:
        def __enter__(self) -> _BoomDDGS:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def text(self, *_a: object, **_k: object) -> list[dict[str, str]]:
            raise RuntimeError("network down")

    monkeypatch.setattr(ddgs_module, "DDGS", _BoomDDGS)

    results = await search_engine.search_duckduckgo("q")

    assert results == []


@pytest.mark.asyncio
async def test_run_search_uses_ddgs_when_no_tavily_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a Tavily key, run_search routes to the DuckDuckGo (ddgs) backend."""
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    called: dict[str, Any] = {}

    async def _fake_ddg(query: str, max_results: int = 5) -> list[dict[str, str]]:
        called["query"] = query
        called["max_results"] = max_results
        return [{"title": "T", "snippet": "S", "url": "https://example.com"}]

    monkeypatch.setattr(search_engine, "search_duckduckgo", _fake_ddg)

    results = await search_engine.run_search("hello", max_results=3)

    assert called == {"query": "hello", "max_results": 3}
    assert results == [{"title": "T", "snippet": "S", "url": "https://example.com"}]
