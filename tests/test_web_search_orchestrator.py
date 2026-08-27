from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plugins.recon.recon_web_search import ReconWebSearchPlugin
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

    # Provide a minimal action catalog so the delivery allowlist derivation
    # (message_* only) has something to produce.
    from core.core_initializer import core_initializer

    monkeypatch.setattr(
        core_initializer,
        "actions_block",
        {
            "available_actions": {
                "search_current_knowledge": {
                    "schema": {"type": "object", "properties": {}, "required": []},
                    "brief": "Search the web.",
                    "source": "web_search_plugin",
                },
                "message_discord_bot": {
                    "schema": {"type": "object", "properties": {}, "required": []},
                    "brief": "Send Discord message.",
                    "source": "message_plugin, discord_bot",
                },
            }
        },
        raising=False,
    )

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
    # Delivery-turn structural scoping (search-loop fix, 2026-08-17): the second
    # turn must be restricted to message_* so the model cannot re-emit the
    # producing search action and loop.
    allowed = ctx.get("allowed_action_types")
    assert isinstance(allowed, list) and len(allowed) > 0
    assert all(str(a).startswith("message_") for a in allowed)


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
    monkeypatch.setattr(search_engine, "search_wikipedia", _empty_results)
    monkeypatch.setattr(search_engine, "search_hackernews", _empty_results)

    results = await search_engine.run_search("hello", max_results=3)

    assert results == []


async def _empty_results(query: str, max_results: int = 5) -> list[dict[str, str]]:
    return []


@pytest.mark.asyncio
async def test_run_search_falls_back_to_wikipedia_when_no_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SearXNG/Tavily -> the keyless Wikipedia tier serves the query."""
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    called: dict[str, Any] = {}

    async def _fake_wiki(query: str, max_results: int = 5) -> list[dict[str, str]]:
        called["query"] = query
        called["max_results"] = max_results
        return [
            {
                "title": "Qwen",
                "snippet": "AI model",
                "url": "https://en.wikipedia.org/wiki/Qwen",
            }
        ]

    monkeypatch.setattr(search_engine, "search_wikipedia", _fake_wiki)

    results = await search_engine.run_search("Qwen release date", max_results=3)

    assert called == {"query": "Qwen release date", "max_results": 3}
    assert results == [
        {
            "title": "Qwen",
            "snippet": "AI model",
            "url": "https://en.wikipedia.org/wiki/Qwen",
        }
    ]


@pytest.mark.asyncio
async def test_run_search_falls_back_to_hackernews_when_wikipedia_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wikipedia returning nothing falls through to the Hacker News tier."""
    monkeypatch.setattr(search_engine, "_searxng_url", lambda: "")
    monkeypatch.setattr(search_engine, "_tavily_api_key", lambda: "")

    async def _empty_wiki(query: str, max_results: int = 5) -> list[dict[str, str]]:
        return []

    called: dict[str, Any] = {}

    async def _fake_hn(query: str, max_results: int = 5) -> list[dict[str, str]]:
        called["query"] = query
        return [
            {
                "title": "Qwen 3.8",
                "snippet": "discussion",
                "url": "https://news.ycombinator.com/item?id=1",
            }
        ]

    monkeypatch.setattr(search_engine, "search_wikipedia", _empty_wiki)
    monkeypatch.setattr(search_engine, "search_hackernews", _fake_hn)

    results = await search_engine.run_search("Qwen release date", max_results=3)

    assert called == {"query": "Qwen release date"}
    assert results == [
        {
            "title": "Qwen 3.8",
            "snippet": "discussion",
            "url": "https://news.ycombinator.com/item?id=1",
        }
    ]


# ── Search-loop hardening tests (2026-08-18) ──────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_submit_dedups_in_flight_same_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second background search for the same interface_path while one is in
    flight is rejected (returns the existing task id) so a single request can
    never stack multiple deliveries — the 'max 2 messages' guarantee."""

    async def _fake_init() -> None:
        return None

    async def _fake_insert(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(search_orchestrator, "_init_table", _fake_init)
    monkeypatch.setattr(search_orchestrator, "_insert_task", _fake_insert)

    created: list[str] = []

    class _DummyTask:
        def __init__(self, name: str) -> None:
            self.name = name

        def add_done_callback(self, cb: Any) -> None:
            pass

    def _fake_create_task(coro: Any) -> _DummyTask:
        created.append("task")
        return _DummyTask("dummy")

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)

    orch = SearchOrchestrator()
    t1 = await orch.submit(interface_path="tg/1", queries=["q1"], search_context="c")
    t2 = await orch.submit(interface_path="tg/1", queries=["q2"], search_context="c2")

    # The second submit returned the existing in-flight task id...
    assert t1 == t2
    # ...and only ONE background task was created.
    assert len(created) == 1


@pytest.mark.asyncio
async def test_orchestrator_submit_allows_distinct_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different conversations are independent: each gets its own task."""

    async def _fake_init() -> None:
        return None

    async def _fake_insert(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(search_orchestrator, "_init_table", _fake_init)
    monkeypatch.setattr(search_orchestrator, "_insert_task", _fake_insert)

    created: list[str] = []

    class _DummyTask:
        def __init__(self, name: str) -> None:
            self.name = name

        def add_done_callback(self, cb: Any) -> None:
            pass

    def _fake_create_task(coro: Any) -> _DummyTask:
        created.append("task")
        return _DummyTask("dummy")

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)

    orch = SearchOrchestrator()
    t1 = await orch.submit(interface_path="tg/1", queries=["q1"], search_context="c")
    t2 = await orch.submit(interface_path="tg/2", queries=["q2"], search_context="c2")

    assert t1 != t2
    assert len(created) == 2


@pytest.mark.asyncio
async def test_recon_skips_when_web_search_task_id_present(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery turn carrying the orchestrator's web_search_task_id marker must
    not trigger a new search."""
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
        context_memory={"web_search_task_id": "t1"},
    )
    assert called["submit"] is False
    assert out == []


@pytest.mark.asyncio
async def test_recon_skips_when_interface_id_is_web_search(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn delivered on the web_search interface must not trigger a search."""
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
        context_memory={"interface_id": "web_search"},
    )
    assert called["submit"] is False
    assert out == []


@pytest.mark.asyncio
async def test_recon_triggered_instruction_carries_structural_marker(
    enable_recon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a background search IS triggered, the returned instruction carries a
    structural web_search_triggered marker so prompt_engine can drop the inline
    search action for that turn."""

    class _FakeOrchestrator:
        async def submit(self, **_k: Any) -> str:
            return "task-xyz"

    monkeypatch.setattr(
        search_orchestrator,
        "get_search_orchestrator",
        lambda: _FakeOrchestrator(),
    )

    plugin = ReconWebSearchPlugin()
    out = await plugin.parse_recon_response(
        {"web_search": ["weather Rome today"]},
        message=_Msg(interface_path="tg/42"),
        context_memory={},
    )
    assert len(out) == 1
    assert out[0]["type"] == "instruction"
    assert out[0].get("web_search_triggered") is True


@pytest.mark.asyncio
async def test_execute_action_refuses_on_delivery_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSearchPlugin.execute_action must NOT run a search on a delivery turn
    (detected structurally via beat_type / web_search_task_id /
    system_message.is_action_result_delivery), so the model cannot re-emit the
    producing action and loop."""
    from plugins.web_search_plugin.web_search_plugin import WebSearchPlugin
    import plugins.web_search_plugin as ws_module

    searched: list[str] = []

    async def _fake_run_search(query: str, max_results: int = 5) -> list[dict]:
        searched.append(query)
        return [{"title": "T", "snippet": "S", "url": "https://example.com"}]

    monkeypatch.setattr(ws_module, "run_search", _fake_run_search)

    plugin = WebSearchPlugin()
    action = {"type": "search_current_knowledge", "payload": {"query": "q"}}

    # Delivery turn flagged via beat_type.
    out = await plugin.execute_action(
        action,
        {"beat_type": "web_search_result"},
        bot=None,
        original_message=None,
    )
    assert out is None
    assert searched == []

    # Delivery turn flagged via web_search_task_id.
    out = await plugin.execute_action(
        action,
        {"web_search_task_id": "t1"},
        bot=None,
        original_message=None,
    )
    assert out is None
    assert searched == []

    # Delivery turn flagged via system_message.is_action_result_delivery.
    out = await plugin.execute_action(
        action,
        {"system_message": {"is_action_result_delivery": True}},
        bot=None,
        original_message=None,
    )
    assert out is None
    assert searched == []

    # A normal turn still runs the search.
    out = await plugin.execute_action(
        action, {"interface_path": "tg/1"}, bot=None, original_message=None
    )
    assert out is not None
    assert searched == ["q"]


@pytest.mark.asyncio
async def test_execute_action_is_pure_tool_when_agent_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When called as an agent tool (context carries ``agent_tool: True``),
    WebSearchPlugin.execute_action must be a *pure tool*: it runs the search and
    returns the results to the bounded loop WITHOUT enqueuing a separate LLM
    delivery turn. The agent loop delivers its single final reply via
    agent_router._deliver_agent_reply, so a per-call delivery here is what
    produced the observed spam (one user request -> many web-search messages)."""
    import json

    from plugins.web_search_plugin.web_search_plugin import WebSearchPlugin
    import plugins.web_search_plugin as ws_module
    import core.auto_response as auto_response

    searched: list[str] = []

    async def _fake_run_search(query: str, max_results: int = 5) -> list[dict]:
        searched.append(query)
        return [
            {"title": "T1", "snippet": "S1", "url": "https://example.com/1"},
            {"title": "T2", "snippet": "S2", "url": "https://example.com/2"},
        ]

    monkeypatch.setattr(ws_module, "run_search", _fake_run_search)

    # request_llm_delivery must NEVER be called on the agent-tool path. If it
    # is, the test fails loudly instead of silently passing.
    async def _fail_if_delivery(*args, **kwargs):
        raise AssertionError(
            "request_llm_delivery must not be called when search runs as an agent tool"
        )

    monkeypatch.setattr(auto_response, "request_llm_delivery", _fail_if_delivery)

    plugin = WebSearchPlugin()
    action = {"type": "search_current_knowledge", "payload": {"query": "q"}}

    out = await plugin.execute_action(
        action,
        {"agent_tool": True, "interface_path": "tg/1"},
        bot=None,
        original_message=None,
    )

    # The search ran once.
    assert searched == ["q"]
    # The tool returned the results to the loop (pure tool), not None.
    assert out is not None
    assert out.get("status") == "ok"
    assert out.get("results_count") == 2
    # The result string carries the search results so the loop can reason on them.
    result_text = out.get("result", "")
    assert isinstance(result_text, str)
    parsed = json.loads(result_text)
    assert len(parsed) == 2
    assert parsed[0]["result"]["title"] == "T1"


@pytest.mark.asyncio
async def test_search_wikipedia_maps_opensearch_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opensearch [query, titles, descriptions, urls] shape maps correctly."""

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list:
            return [
                "Qwen",
                ["Qwen", "Qwen2"],
                ["AI model", ""],
                [
                    "https://en.wikipedia.org/wiki/Qwen",
                    "https://en.wikipedia.org/wiki/Qwen2",
                ],
            ]

    calls: list[Any] = []

    def _fake_get(url: str, headers: Any, params: Any, timeout: float) -> _FakeResp:
        calls.append({"url": url, "params": params})
        return _FakeResp()

    monkeypatch.setattr(search_engine.requests, "get", _fake_get)

    out = await search_engine.search_wikipedia("Qwen", max_results=2)

    assert calls[0]["url"] == "https://en.wikipedia.org/w/api.php"
    assert calls[0]["params"]["action"] == "opensearch"
    assert out == [
        {
            "title": "Qwen",
            "snippet": "AI model",
            "url": "https://en.wikipedia.org/wiki/Qwen",
        },
        {"title": "Qwen2", "snippet": "", "url": "https://en.wikipedia.org/wiki/Qwen2"},
    ]


@pytest.mark.asyncio
async def test_search_hackernews_maps_comment_hit_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Algolia comment hits (story_title/story_url) map to title/snippet/url."""

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "hits": [
                    {
                        "title": "Qwen 3.8",
                        "url": "https://twitter.com/Alibaba_Qwen/status/1",
                        "story_text": "announcement",
                    },
                    {
                        "title": None,
                        "story_title": "Qwen 3.8",
                        "story_url": None,
                        "objectID": "42",
                        "comment_text": "discussion",
                    },
                ]
            }

    calls: list[Any] = []

    def _fake_get(url: str, headers: Any, params: Any, timeout: float) -> _FakeResp:
        calls.append({"url": url, "params": params})
        return _FakeResp()

    monkeypatch.setattr(search_engine.requests, "get", _fake_get)

    out = await search_engine.search_hackernews("Qwen", max_results=2)

    assert calls[0]["url"] == "https://hn.algolia.com/api/v1/search"
    assert calls[0]["params"]["hitsPerPage"] == 2
    assert out == [
        {
            "title": "Qwen 3.8",
            "snippet": "announcement",
            "url": "https://twitter.com/Alibaba_Qwen/status/1",
        },
        {
            "title": "Qwen 3.8",
            "snippet": "discussion",
            "url": "https://news.ycombinator.com/item?id=42",
        },
    ]


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

    def _valid(i: int) -> dict[str, str]:
        return {"title": f"v{i}", "snippet": "ok", "url": f"https://v{i}.x"}

    def _blocked(i: int) -> dict[str, str]:
        return {"title": f"b{i}", "snippet": "", "url": f"https://b{i}.x"}

    pages = [
        [_blocked(0), _blocked(1), _blocked(2), _blocked(3), _valid(0)],
        [_valid(1), _valid(2), _valid(3), _valid(4), _valid(5)],
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
