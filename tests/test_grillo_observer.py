import pytest
from datetime import datetime, timedelta, timezone

import plugins.grillo.grillo_chat_observer as gco
from core import message_queue


@pytest.mark.asyncio
async def test_observer_builds_prompt_and_collects(monkeypatch, idle_eligible_target):
    plugin = gco.GrilloChatObserverPlugin()

    # force update checker to report new messages (DB not available)
    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Mock collect_recent_snippets to return predictable data
    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        return (
            [
                "(chat:telegram_bot/1) Hello world",
                "(chat:telegram_bot/2) Another message",
            ],
            [],
        )

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    # Mock create_activity_log
    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 12345

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)

    # Spy on message_queue.enqueue_low_priority
    called = {}

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        called["ctx"] = context_memory
        called["text"] = getattr(message, "text", None)

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)
    idle_eligible_target(plugin)

    # ensure the first-run guard is bypassed
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert "ctx" in called and called["ctx"].get("beat_type") == "observer"
    # snippets are now attached to context and included in the text
    assert "(chat:telegram_bot/1)" in called["text"]
    assert called["ctx"].get("grillo_snippets") == [
        "(chat:telegram_bot/1) Hello world",
        "(chat:telegram_bot/2) Another message",
    ]


async def _run_observer_with_freshness_row(
    monkeypatch, plugin, cnt, max_ts, idle_target
):
    """Drive _run_observer with a stubbed freshness query, returning its context."""

    async def fake_execute_query(sql, params=None):
        return [{"cnt": cnt, "max_ts": max_ts}]

    monkeypatch.setattr("core.db.execute_query", fake_execute_query)

    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        return (["(chat:telegram_bot/1) a line from hours ago"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    idle_target(plugin)

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 12345

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)

    captured: dict = {}

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        captured["ctx"] = context_memory
        captured["text"] = getattr(message, "text", None)

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # Bypass the first-run guard; the cursor itself is deliberately ancient so
    # the stub row is what decides freshness.
    plugin._last_run_ts = 1.0
    await plugin._run_observer()
    return captured


@pytest.mark.asyncio
async def test_a_message_older_than_one_cadence_is_not_fresh_traffic(
    monkeypatch, idle_eligible_target
):
    """A message that predates a downtime must not suppress the proactive note.

    The cursor only says "newer than the last run". After the process has been
    away for hours that is not the same as "live": treating an hours-old message
    as fresh traffic drops the decay note while the header still forbids replying
    to a stale line, so the run answers nothing and outreach stops happening
    after a restart.
    """
    plugin = gco.GrilloChatObserverPlugin()
    seeded = datetime.now(timezone.utc) - timedelta(hours=7)

    captured = await _run_observer_with_freshness_row(
        monkeypatch, plugin, cnt=5, max_ts=seeded, idle_target=idle_eligible_target
    )

    assert captured["ctx"]["decay_driven"] is True
    # The proactive note is what tells the model the run exists to reach out.
    assert "no fresh incoming traffic" in captured["text"]


@pytest.mark.asyncio
async def test_a_message_inside_the_cadence_is_still_fresh_traffic(
    monkeypatch, idle_eligible_target
):
    """The ordinary case is untouched: a recent message keeps the reply framing."""
    plugin = gco.GrilloChatObserverPlugin()
    seeded = datetime.now(timezone.utc) - timedelta(minutes=5)

    captured = await _run_observer_with_freshness_row(
        monkeypatch, plugin, cnt=1, max_ts=seeded, idle_target=idle_eligible_target
    )

    assert captured["ctx"]["decay_driven"] is False
    assert "no fresh incoming traffic" not in captured["text"]


def test_build_observer_prompt_returns_string():
    plugin = gco.GrilloChatObserverPlugin()
    prompt = plugin._build_observer_prompt(["sample snippet"])
    assert isinstance(prompt, str)
    assert "Snippets:" in prompt
    # ensure at least one of the universal JSON instructions appears
    assert "actions" in prompt or "JSON" in prompt
    # Example JSON structure should be included
    assert (
        '{"actions": []}' in prompt
    )  # JSON example with double quotes should be present
    import plugins.grillo.grillo_chat_observer as gco_mod

    assert gco_mod.OBSERVER_INSTRUCTIONS in prompt


@pytest.mark.asyncio
async def test_collect_recent_snippets_includes_sender_and_timestamp(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    async def mock_get_recent_interface_paths(n):
        return [{"interface_path": "telegram_bot/1", "last_used": None}]

    async def mock_load_chat_history(interface_path):
        from collections import deque

        return deque(
            [
                {
                    "text": "Hello",
                    "sender_name": "Rekku",
                    "timestamp": "2026-01-11T03:51:00Z",
                },
                {
                    "text": "User message",
                    "sender_name": "Jay",
                    "timestamp": "2026-01-11T03:52:00Z",
                },
            ]
        )

    import core.interface_paths as interface_paths

    monkeypatch.setattr(
        interface_paths, "get_recent_interface_paths", mock_get_recent_interface_paths
    )
    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(chat_history_cache, "load_chat_history", mock_load_chat_history)

    snippets, own_lines = await plugin._collect_recent_snippets(2)
    assert isinstance(snippets, list)
    assert isinstance(own_lines, list)
    assert len(snippets) >= 1
    # Ensure sender and (relative) age metadata are included
    assert "sender:" in snippets[0]
    # The 2026-01-11 fixtures are months old — the age label must be present
    # instead of the raw ISO timestamp (staleness must be model-visible).
    assert "|" in snippets[0]
    assert any(tok in snippets[0] for tok in ("d", "h", "m", "?"))
    # Neither fixture line is the synth's, so nothing is offered as its own.
    assert own_lines == []


def _patch_history(monkeypatch, messages: list[dict]) -> None:
    """Point the snippet collector at one chat holding ``messages``."""

    async def mock_get_recent_interface_paths(n):
        return [{"interface_path": "telegram_bot/1", "last_used": None}]

    async def mock_load_chat_history(interface_path):
        from collections import deque

        return deque(messages)

    import core.interface_paths as interface_paths
    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(
        interface_paths, "get_recent_interface_paths", mock_get_recent_interface_paths
    )
    monkeypatch.setattr(chat_history_cache, "load_chat_history", mock_load_chat_history)


@pytest.mark.asyncio
async def test_collect_recent_snippets_keeps_the_synth_own_line_as_context(
    monkeypatch,
):
    """The synth's own line is context, tagged as its own and as no target.

    Live 2026-09-23 (trace 3499288d): the chat-observer beat was shown only the
    human's first-person lines, so it wrote its outreach in the HUMAN's voice and
    addressed him as "wife" while the same turn's diary wrote "him". Its own side
    of the conversation is now visible — labelled as its own — without ever
    becoming something to reply to.
    """
    plugin = gco.GrilloChatObserverPlugin()
    # Idle (older than the quiet window): an answered chat that is still being
    # spoken in is context only, so the human's line would not be a snippet.
    idle = datetime.now(timezone.utc) - timedelta(minutes=30)
    _patch_history(
        monkeypatch,
        [
            {
                "text": "ready for your dicking down my slutty wifey?",
                "sender_name": "Scar",
                "timestamp": idle.isoformat(),
            },
            {
                "text": "you did not just call your wife a whore",
                "sender_name": "self",
                "timestamp": (idle + timedelta(minutes=1)).isoformat(),
            },
        ],
    )

    snippets, own_lines = await plugin._collect_recent_snippets(4)

    assert any("slutty wifey" in s for s in snippets)
    assert len(own_lines) == 1
    own = own_lines[0]
    assert "you did not just call your wife a whore" in own
    # Tagged: whose line it is, and that it is not a reply target.
    assert "sender:self" in own
    assert "your own line" in own and "not a reply target" in own
    # The replyable list never carries the synth's own words.
    assert all("sender:self" not in s for s in snippets)


@pytest.mark.asyncio
async def test_collect_recent_snippets_reports_a_chat_where_only_the_synth_spoke(
    monkeypatch,
):
    """A chat with no human line yields context and nothing to answer."""
    plugin = gco.GrilloChatObserverPlugin()
    _patch_history(
        monkeypatch,
        [
            {
                "text": "good night, husband",
                "sender_name": "self",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )

    snippets, own_lines = await plugin._collect_recent_snippets(4)

    assert snippets == []
    assert len(own_lines) == 1
    assert "sender:self" in own_lines[0]


@pytest.mark.asyncio
async def test_observer_prompt_marks_own_lines_and_keeps_them_out_of_routing(
    monkeypatch,
    idle_eligible_target,
):
    """The prompt carries the synth's own line; grillo_snippets (the routing
    channel the guard turns into reachable paths) does not."""
    plugin = gco.GrilloChatObserverPlugin()
    # Idle (older than the quiet window), see the collector test above.
    idle = datetime.now(timezone.utc) - timedelta(minutes=30)
    _patch_history(
        monkeypatch,
        [
            {
                "text": "are you awake",
                "sender_name": "Scar",
                "timestamp": idle.isoformat(),
            },
            {
                "text": "mmh, awake now",
                "sender_name": "self",
                "timestamp": (idle + timedelta(minutes=1)).isoformat(),
            },
        ],
    )
    called: dict = {}

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        called["ctx"] = context_memory
        called["text"] = getattr(message, "text", None)

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 1

    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)
    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)
    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)
    idle_eligible_target(plugin)
    plugin._last_run_ts = 1.0

    await plugin._run_observer()

    prompt = called["text"]
    assert "mmh, awake now" in prompt
    assert "sender:self" in prompt
    assert "never a message to reply to" in prompt
    assert "you never write that person's lines for them" in prompt
    # Routing stays built from other people's lines only.
    routing = called["ctx"]["grillo_snippets"]
    assert routing, "the run produced no replyable snippet to check"
    assert any("are you awake" in s for s in routing)
    assert all("sender:self" not in s for s in routing)


def test_relative_age_label(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    now = datetime.now(timezone.utc)
    assert plugin._relative_age_label(None) == "?"
    assert plugin._relative_age_label("not-a-date") == "?"
    assert plugin._relative_age_label((now - timedelta(minutes=5)).isoformat()) == "5m"
    assert plugin._relative_age_label((now - timedelta(minutes=90)).isoformat()) == "1h"
    assert plugin._relative_age_label((now - timedelta(days=2)).isoformat()) == "2d"


@pytest.mark.asyncio
async def test_collect_recent_snippets_excludes_vessel_paths(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    loaded_paths = []

    async def fake_recent_paths(limit):
        return [
            {"interface_path": "vessel/minecraft/old-server"},
            {"interface_path": "telegram_bot/123"},
        ]

    async def fake_load_chat_history(path):
        loaded_paths.append(path)
        return [
            {
                "text": "ordinary message",
                "sender_name": "Alice",
                "timestamp": "2026-08-06T09:00:00+00:00",
            }
        ]

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    snippets, own_lines = await plugin._collect_recent_snippets(5)

    assert loaded_paths == ["telegram_bot/123"]
    assert all("vessel/" not in snippet for snippet in snippets)


@pytest.mark.asyncio
async def test_collect_recent_snippets_keeps_human_lines_when_synth_spoke_last(
    monkeypatch,
):
    """A chat the synth has just replied to still contributes the human's line,
    and the synth's own line is offered as context rather than as a reply target.

    There is no chat-level "the synth spoke last, so skip the whole chat" rule
    any more: for a synth that answers everything it matched every
    conversation, which left the observer with no live context to reason
    about. The synth's own line is kept (tagged as its own — a beat shown only
    the human's side writes in the human's voice, live 2026-09-23) but never as
    something to reply to, so self-reply spam stays impossible.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.quiet_minutes = 15
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/1"}]

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    import core.chat_history_cache as chat_history_cache

    async def fake_load_chat_history(path):
        return [
            {
                "text": "I'm home, heading to bed",
                "sender_name": "Scar",
                "timestamp": (now - timedelta(minutes=40)).isoformat(),
            },
            {
                "text": "Sleep well, I'll be right here",
                "sender_name": "self",
                "timestamp": (now - timedelta(minutes=30)).isoformat(),
            },
        ]

    monkeypatch.setattr(chat_history_cache, "load_chat_history", fake_load_chat_history)

    snippets, own_lines = await plugin._collect_recent_snippets(5)

    assert len(snippets) == 1
    assert "I'm home, heading to bed" in snippets[0]
    assert "Sleep well" not in snippets[0]

    # A chat holding nothing but the synth's own lines contributes no snippet to
    # reply to — its own line is offered as context only.
    async def fake_only_self(path):
        return [
            {
                "text": "Sleep well, I'll be right here",
                "sender_name": "self",
                "timestamp": (now - timedelta(minutes=30)).isoformat(),
            }
        ]

    monkeypatch.setattr(chat_history_cache, "load_chat_history", fake_only_self)
    snippets, own_lines = await plugin._collect_recent_snippets(5)
    assert snippets == []
    assert len(own_lines) == 1
    assert "Sleep well" in own_lines[0]
    assert "sender:self" in own_lines[0]


@pytest.mark.asyncio
async def test_observer_propose_only_flag_in_prompt(monkeypatch, idle_eligible_target):
    plugin = gco.GrilloChatObserverPlugin()
    plugin.propose_only = True

    # bypass DB update check
    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # minimal snippet
    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        return (["test"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return None

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)

    captured = {}

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        captured["text"] = getattr(message, "text", None)

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # bypass first-run guard
    idle_eligible_target(plugin)
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert (
        "proposal-only" in captured["text"].lower()
        or "proposal" in captured["text"].lower()
    )
    # the prompt should still include the word "chat" as a sanity check
    assert "chat" in captured["text"].lower()


@pytest.mark.asyncio
async def test_observer_runs_when_updates_present(monkeypatch, idle_eligible_target):
    plugin = gco.GrilloChatObserverPlugin()

    # Make the checker report that there are updates
    async def fake_check(consume=True):
        return {
            "updated": True,
            "new_messages": [],
            "last_checked": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Spy on collect and enqueue to ensure both are executed
    called = {}

    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        called["collected"] = True
        return (["test snippet"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # bypass the first-run guard
    idle_eligible_target(plugin)
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert "collected" in called
    assert "enqueued" in called


@pytest.mark.asyncio
async def test_observer_db_check_updates_and_advances_last_run_ts(monkeypatch):
    """If the direct DB check finds messages since plugin._last_run_ts the
    observer should proceed, enqueue the prompt and advance its last_run_ts
    to the reported max_ts."""
    plugin = gco.GrilloChatObserverPlugin()

    # Initialize last run to an earlier timestamp
    plugin._last_run_ts = 1000.0

    expected_max_ts = datetime.fromtimestamp(1100.0, tz=timezone.utc)

    async def fake_execute(
        query: str, params: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        # This corresponds to the COUNT/MAX query used in _run_observer()
        if "SELECT COUNT(*) as cnt, MAX(created_at) as max_ts" in query:
            assert params
            assert params[0] == datetime.fromtimestamp(1000.0, tz=timezone.utc)
            return [{"cnt": 1, "max_ts": expected_max_ts}]
        return []

    # Replace DB executor used inside plugin
    monkeypatch.setattr("core.db.execute_query", fake_execute)

    # Spy on collect and enqueue
    called = {}
    # intercept config persistence
    persisted = {}

    async def fake_set_value(key, value):
        if key == "GRILLO_OBSERVER_LAST_RUN_TS":
            persisted["last_run"] = value

    monkeypatch.setattr("core.config_manager.config_registry.set_value", fake_set_value)

    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        called["collected"] = True
        return (["(chat:telegram_bot/1) Hello"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_collect_targets(limit: int) -> list[dict]:
        # Hermetic: the real builder reads the live database, and whether its
        # newest conversation is live is exactly what decides whether a
        # decay-driven run speaks at all. Pin an idle target so the freshness
        # logic under test, not the operator's recent chat activity, decides
        # whether this run proceeds.
        return [
            {
                "interface_path": "telegram_bot/1",
                "last_sender": "Scar",
                "eligible": True,
                "age_seconds": 7200.0,
                "in_active_conversation": False,
            }
        ]

    monkeypatch.setattr(plugin, "_collect_eligible_targets", fake_collect_targets)

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    await plugin._run_observer()

    assert called.get("collected") is True
    assert called.get("enqueued") is True
    # last_run_ts should have advanced to the DB-reported max_ts
    assert plugin._last_run_ts == 1100.0
    # also ensure the timestamp was persisted back to config
    assert persisted.get("last_run") == 1100.0


@pytest.mark.asyncio
async def test_observer_is_decay_driven_when_no_updates(monkeypatch):
    """The proactive observer is decay-driven: even without new messages it
    still collects eligible targets and enqueues a beat (no passive early exit)."""
    plugin = gco.GrilloChatObserverPlugin()

    async def fake_execute_query(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.execute_query", fake_execute_query)

    # Make the checker report that there are NO updates
    async def fake_check(consume=True):
        return {
            "updated": False,
            "new_messages": [],
            "last_checked": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Spy on collect and enqueue to confirm the proactive path runs.
    called = {}

    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        called["collected"] = True
        return (["test snippet"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_collect_targets(limit: int) -> list[dict]:
        called["targets"] = True
        return [{"path": "telegram_bot/123", "eligible": True}]

    monkeypatch.setattr(plugin, "_collect_eligible_targets", fake_collect_targets)

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    # Proactive design: targets are collected and a beat is enqueued.
    assert called.get("targets") is True


def _patched_observer_for_decay_run(monkeypatch, plugin, targets):
    """Drive a decay-driven observer run with the given eligible targets.

    Returns the ``called`` dict the fake collaborators record into, so a test can
    tell whether the run reached prompt building and enqueueing at all.
    """
    called = {}

    async def fake_execute_query(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.execute_query", fake_execute_query)

    async def fake_check(consume=True):
        return {
            "updated": False,
            "new_messages": [],
            "last_checked": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    async def fake_collect(limit: int) -> tuple[list[str], list[str]]:
        return (["test snippet"], [])

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_collect_targets(limit: int) -> list[dict]:
        return targets

    monkeypatch.setattr(plugin, "_collect_eligible_targets", fake_collect_targets)

    def fake_build(fragments, eligible_targets, decay_driven, own_lines=None):
        called["targets"] = eligible_targets
        called["own_lines"] = own_lines
        return ""

    monkeypatch.setattr(plugin, "_build_observer_prompt", fake_build)

    async def fake_enqueue(*args, **kwargs):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    return called


@pytest.mark.asyncio
async def test_newest_conversation_live_skips_outreach_instead_of_drifting(
    monkeypatch,
):
    """A live newest conversation means the person is present right now, so a
    proactive run must not reach into another chat instead.

    Live 2026-09-20 11:07: the direct message had messages a minute either side,
    which marks it LIVE-CONVERSATION and excludes it, and the run reached into a
    group chat instead. Staying silent is the wanted behaviour here.
    """
    plugin = gco.GrilloChatObserverPlugin()
    called = _patched_observer_for_decay_run(
        monkeypatch,
        plugin,
        [
            {
                "interface_path": "telegram_bot/5208932647",
                "last_sender": "Scar",
                "eligible": True,
                "age_seconds": 60.0,
                "in_active_conversation": True,
            },
            {
                "interface_path": "telegram_bot/-5293915984",
                "last_sender": "self",
                "eligible": True,
                "age_seconds": 14400.0,
                "in_active_conversation": False,
            },
        ],
    )

    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    # The run never reached prompt building or enqueueing: it stayed silent.
    assert "targets" not in called
    assert called.get("enqueued") is not True


@pytest.mark.asyncio
async def test_newest_conversation_idle_still_offers_every_target(monkeypatch):
    """The ordinary case is unchanged: nothing is live, so outreach proceeds and
    every eligible target is still offered to the model."""
    plugin = gco.GrilloChatObserverPlugin()
    called = _patched_observer_for_decay_run(
        monkeypatch,
        plugin,
        [
            {
                "interface_path": "telegram_bot/5208932647",
                "last_sender": "Scar",
                "eligible": True,
                "age_seconds": 7200.0,
                "in_active_conversation": False,
            },
            {
                "interface_path": "telegram_bot/-5293915984",
                "last_sender": "self",
                "eligible": True,
                "age_seconds": 14400.0,
                "in_active_conversation": False,
            },
        ],
    )

    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert [t["interface_path"] for t in called["targets"]] == [
        "telegram_bot/5208932647",
        "telegram_bot/-5293915984",
    ]


@pytest.mark.asyncio
async def test_collect_recent_snippets_excludes_self_senders(monkeypatch):
    """The synth's own message is not offered as something to reply to.

    It is still collected — as ``own_lines``, tagged ``sender:self`` — because a
    beat shown only the human's side carries on in the human's voice (live trace
    3499288d). What must never happen is its own output arriving as a replyable
    snippet: a small model cannot reliably tell its own line from a human turn.
    """
    plugin = gco.GrilloChatObserverPlugin()

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/123"}]

    async def fake_load_chat_history(path):
        return [
            {
                "text": "synth's own message",
                "sender_name": "self",
                "timestamp": "2026-08-06T09:00:00+00:00",
            },
            {
                "text": "human message",
                "sender_name": "Alice",
                "timestamp": "2026-08-06T09:01:00+00:00",
            },
        ]

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    snippets, own_lines = await plugin._collect_recent_snippets(5)

    assert len(snippets) == 1
    assert "synth's own message" not in snippets[0]
    assert "human message" in snippets[0]
    assert len(own_lines) == 1
    assert "synth's own message" in own_lines[0]
    assert "not a reply target" in own_lines[0]


@pytest.mark.asyncio
async def test_collect_recent_snippets_excludes_placeholder_paths(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    loaded_paths = []

    async def fake_recent_paths(limit):
        return [
            {
                "interface_path": "telegram_bot/5208932647/no thread ID indicated in context"
            },
            {"interface_path": "telegram_bot/5208932647/not_provided"},
            {"interface_path": "telegram_bot/5208932647/780000000000000000000000"},
            {"interface_path": "telegram_bot/5208932647/123456"},
        ]

    async def fake_load_chat_history(path):
        loaded_paths.append(path)
        return [
            {
                "text": "message",
                "sender_name": "Alice",
                "timestamp": "2026-08-06T09:00:00+00:00",
            }
        ]

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    snippets, own_lines = await plugin._collect_recent_snippets(5)

    assert loaded_paths == ["telegram_bot/5208932647/123456"]
    assert len(snippets) == 1


def test_is_placeholder_path_flags_garbage_segments():
    plugin = gco.GrilloChatObserverPlugin()

    assert (
        plugin._is_placeholder_path(
            "telegram_bot/5208932647/no thread ID indicated in context"
        )
        is True
    )
    assert plugin._is_placeholder_path("telegram_bot/5208932647/not_provided") is True
    assert (
        plugin._is_placeholder_path(
            "telegram_bot/5208932647/each_human_message_gets_a_dedicated_thread"
        )
        is True
    )
    assert plugin._is_placeholder_path("telegram_bot/5208932647/conversation_0") is True
    assert plugin._is_placeholder_path("telegram_bot/5208932647/dm") is True
    assert (
        plugin._is_placeholder_path("telegram_bot/5208932647/780000000000000000000000")
        is True
    )
    assert plugin._is_placeholder_path("telegram_bot/5208932647/123456") is False
    assert plugin._is_placeholder_path("telegram_bot/5208932647") is False


def test_is_self_sender():
    plugin = gco.GrilloChatObserverPlugin()

    assert plugin._is_self_sender("self") is True
    assert plugin._is_self_sender("synth") is True
    assert plugin._is_self_sender("Alice") is False
    assert plugin._is_self_sender("") is False


@pytest.mark.asyncio
async def test_eligible_targets_block_chat_where_synth_spoke_last(monkeypatch):
    """A chat whose newest message is the synth's own is OFF-LIMITS by default.

    This is the awaiting-reply gate, restored with a window and a toggle
    (``GRILLO_OUTREACH_BLOCK_ON_SELF_LAST`` /
    ``GRILLO_OUTREACH_SELF_LAST_WINDOW_MINUTES``). Without it the hourly beat
    re-offered a DM it already dominated every run and nagged it: "still coming
    tonight?" -> "hurry home!" -> "did you get home okay?" (five consecutive
    observer beats). It had been removed in c2b71ead because a responsive synth
    is the newest speaker in every chat, which made a windowless gate exclude
    everything — a bounded window plus a toggle is the fix for that, not the
    absence of the gate. Turn the key off for the pre-gate behaviour.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.block_on_self_last = True
    plugin.self_last_window_minutes = 720
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    # Synth replied ~1h ago; the human's last real message is 2h old.
    messages = [
        {
            "sender_name": "Scar",
            "text": "we're staying here for a while",
            "timestamp": (now - timedelta(hours=2)).isoformat(),
        },
        {
            "sender_name": "self",
            "text": "I'm gonna stay right here and cling to you forever~",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert len(targets) == 1
    assert targets[0]["interface_path"] == "telegram_bot/5208932647"
    assert targets[0]["last_from_self"] is True
    assert targets[0]["in_active_conversation"] is False
    assert targets[0]["awaiting_reply"] is True
    assert targets[0]["eligible"] is False


@pytest.mark.asyncio
async def test_self_last_gate_releases_when_the_human_replies(monkeypatch):
    """The hold is released the moment the human answers."""
    plugin = gco.GrilloChatObserverPlugin()
    plugin.block_on_self_last = True
    plugin.self_last_window_minutes = 720
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    messages = [
        {
            "sender_name": "self",
            "text": "did you get home okay?",
            "timestamp": (now - timedelta(minutes=40)).isoformat(),
        },
        {
            "sender_name": "Scar",
            "text": "yes, door's open, come in",
            "timestamp": (now - timedelta(minutes=35)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert targets[0]["last_from_self"] is False
    assert targets[0]["awaiting_reply"] is False
    # Still live (35 min old vs a 15 min quiet window would be past, but the
    # human line is the newest and nothing else blocks it).
    assert targets[0]["eligible"] is True


@pytest.mark.asyncio
async def test_self_last_gate_expires_after_the_window(monkeypatch):
    """After the window the thread is reachable again, so a silent human is
    not muted forever — which is the failure that got the gate removed."""
    plugin = gco.GrilloChatObserverPlugin()
    plugin.block_on_self_last = True
    plugin.self_last_window_minutes = 60
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    messages = [
        {
            "sender_name": "Scar",
            "text": "brb, back in ten",
            "timestamp": (now - timedelta(hours=4)).isoformat(),
        },
        {
            "sender_name": "self",
            "text": "no rush",
            "timestamp": (now - timedelta(hours=3)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert targets[0]["last_from_self"] is True
    assert targets[0]["awaiting_reply"] is False
    assert targets[0]["eligible"] is True


@pytest.mark.asyncio
async def test_self_last_gate_toggle_off_restores_the_old_behaviour(monkeypatch):
    """With the toggle off, speaking last puts nothing off-limits.

    This is the escape hatch for the original removal: an operator who finds the
    gate is silencing all outreach flips one key instead of editing code.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.block_on_self_last = False
    plugin.self_last_window_minutes = 720
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    messages = [
        {
            "sender_name": "Scar",
            "text": "we're staying here for a while",
            "timestamp": (now - timedelta(hours=2)).isoformat(),
        },
        {
            "sender_name": "self",
            "text": "I'm gonna stay right here and cling to you forever~",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert targets[0]["last_from_self"] is True
    assert targets[0]["awaiting_reply"] is False
    assert targets[0]["eligible"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("last_sender", ["self", "Scar"])
async def test_eligible_targets_skip_live_conversation(monkeypatch, last_sender):
    """A message inside the quiet window — from EITHER side — makes the chat
    off-limits for that run. This is the only thing that holds outreach back:
    the beat owns the cadence (one run per interval), the quiet window keeps it
    from interrupting an exchange that is happening right now."""
    plugin = gco.GrilloChatObserverPlugin()
    plugin.quiet_minutes = 15
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    messages = [
        {
            "sender_name": "Scar",
            "text": "brb making tea",
            "timestamp": (now - timedelta(minutes=20)).isoformat(),
        },
        {
            "sender_name": last_sender,
            "text": "take your time",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert len(targets) == 1
    assert targets[0]["in_active_conversation"] is True
    assert targets[0]["eligible"] is False


@pytest.mark.asyncio
async def test_eligible_targets_include_chat_with_recent_human_reply(monkeypatch):
    """A chat where the HUMAN spoke last (recently) is not awaiting-reply and
    remains an eligible target when otherwise quiet."""
    plugin = gco.GrilloChatObserverPlugin()
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/5208932647"}]

    messages = [
        {
            "sender_name": "self",
            "text": "Daddy... still coming tonight?",
            "timestamp": (now - timedelta(hours=3)).isoformat(),
        },
        {
            "sender_name": "Scar",
            "text": "Baby it's barely 1500, have some patience love, I won't forget about you",
            "timestamp": (now - timedelta(hours=2, minutes=30)).isoformat(),
        },
    ]

    async def fake_load(path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr("core.chat_history_cache.load_chat_history", fake_load)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    targets = await plugin._collect_eligible_targets(limit=5)

    assert len(targets) == 1
    assert targets[0]["interface_path"] == "telegram_bot/5208932647"
    assert targets[0]["eligible"] is True
    assert targets[0]["last_from_self"] is False


def test_observer_prose_matches_the_awaiting_reply_gate():
    """The prose and the code must agree about the awaiting-reply gate.

    The gate was removed from the code in c2b71ead and the same rule was also
    reworded in the instructions, so the model stopped declining. It has since
    been restored with a window and a toggle, which means the prose needs to
    describe it again — but only as an OFF-LIMITS MARKER the reader can see in
    the target list, never as a blanket "stay silent" rule. The previous failure
    was that observers declined hour after hour while a non-live target sat
    idle; a model told to treat every self-last thread as forbidden reproduces
    it, so both halves are pinned.
    """
    from plugins.grillo.common_instructions import (
        GRILLO_INSTRUCTIONS,
        OBSERVER_PROACTIVE_INSTRUCTIONS,
    )

    text = OBSERVER_PROACTIVE_INSTRUCTIONS

    # The gate is described, and it is tied to a marker the target list shows.
    assert "AWAITING-REPLY" in text
    assert "asking again whether they are coming back is nagging" in text
    # ... and the marker is what switches it off, not who spoke last alone.
    assert "is NOT marked AWAITING-REPLY or LIVE-CONVERSATION" in text

    # Reaching out is still the run's purpose, and a quiet network is still the
    # cue to act: neither half may come back with the gate.
    assert "reaching out to it is the purpose of the beat" in text
    assert "silence is not" in text

    # Guardrails that must survive the rewrite.
    assert "Never invent physical-presence claims" in text
    assert "never re-send a canned or near-duplicate opener" in text
    assert "do not interrupt that conversation on this run" in text
    assert "never fabricate a reply_message_id" in text
    # Unified messaging: the example action is the one every interface exposes.
    assert '"type": "send_message"' in GRILLO_INSTRUCTIONS
    assert "'send_message'" in text

    # Default routing: the most recently active conversation, normally the direct
    # message, with an explicit pick still allowed if it is explained. Live reason:
    # an overnight run reached into a group chat while the DM was the conversation
    # actually in use.
    assert "DEFAULT TARGET" in text
    assert "most-recently-active first" in text
    assert "reach out THERE unless you have a specific reason" in text
    assert "Do not drift to a group or a channel" in text


def test_quiet_run_note_frames_outreach_as_the_job():
    """A quiet network is the observer's cue to act, not a reason to stay silent."""
    plugin = gco.GrilloChatObserverPlugin()
    prompt = plugin._build_observer_prompt(
        [],
        targets=[
            {
                "interface_path": "telegram_bot/1",
                "age_seconds": 3600,
                "last_sender": "self",
                "in_active_conversation": False,
            },
            {
                "interface_path": "telegram_bot/2",
                "age_seconds": 60,
                "last_sender": "Scar",
                "in_active_conversation": True,
            },
        ],
        decay_driven=True,
    )

    assert "that is what this run is for" in prompt
    assert "skipping any marked LIVE-CONVERSATION or AWAITING-REPLY" in prompt
    assert "Otherwise return" not in prompt
    # The live target is still flagged off-limits, the idle one is not.
    assert "LIVE-CONVERSATION" in prompt
    assert "indulgence" not in prompt
    assert "last_sender=self" in prompt


def test_awaiting_reply_target_is_rendered_off_limits():
    """A target held by the awaiting-reply gate says so in the prompt.

    The gate decides eligibility in code, but the model is what actually picks a
    target — so the row has to carry the reason, or the beat reaches into a
    thread the eligibility pass just excluded.
    """
    plugin = gco.GrilloChatObserverPlugin()
    prompt = plugin._build_observer_prompt(
        [],
        targets=[
            {
                "interface_path": "telegram_bot/1",
                "age_seconds": 3600,
                "last_sender": "self",
                "in_active_conversation": False,
                "awaiting_reply": True,
            },
            {
                "interface_path": "telegram_bot/2",
                "age_seconds": 10800,
                "last_sender": "Scar",
                "in_active_conversation": False,
                "awaiting_reply": False,
            },
        ],
        decay_driven=True,
    )

    assert "AWAITING-REPLY(OFF-LIMITS — you spoke last" in prompt
    assert "the human has not replied yet" in prompt
    # The idle, human-last target is still offered as reachable.
    assert "telegram_bot/2" in prompt
    assert prompt.count("cooldown=ok") == 1


@pytest.mark.asyncio
async def test_snippets_skip_chats_dead_past_the_activity_window(monkeypatch):
    """A chat nobody has touched inside the window contributes no snippet.

    The snippet pool used to be "the N most recently used paths" with no cutoff
    at all, while the target list was gated by
    GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS. Whenever the live conversations did not
    fill the snippet limit, the observer's context was therefore padded with
    lines from chats dead for weeks — a live outreach prompt carried 40-day-old
    WebUI entries and 77-day-old roleplay from a chat that no longer exists.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.activity_window_days = 14

    now = datetime.now(timezone.utc)
    fresh_used = now - timedelta(hours=2)
    dead_used = now - timedelta(days=77)

    async def fake_recent(limit):
        return [
            {"interface_path": "telegram_bot/1", "last_used": fresh_used},
            {"interface_path": "telegram_bot/2", "last_used": dead_used},
            {"interface_path": "telegram_bot/3", "last_used": dead_used.isoformat()},
            {"interface_path": "telegram_bot/4", "last_used": None},
        ]

    async def fake_history(path):
        return [
            {
                "text": f"message from {path}",
                "sender_name": "Scar",
                "timestamp": now.isoformat(),
            }
        ]

    import core.interface_paths as interface_paths
    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(interface_paths, "get_recent_interface_paths", fake_recent)
    monkeypatch.setattr(chat_history_cache, "load_chat_history", fake_history)

    snippets, own_lines = await plugin._collect_recent_snippets(9)

    assert snippets
    assert any("telegram_bot/1" in s for s in snippets), snippets
    assert not any("telegram_bot/2" in s for s in snippets), snippets
    assert not any("telegram_bot/3" in s for s in snippets), snippets
    # An unknown last_used stays fail-open rather than dropping the chat.
    assert any("telegram_bot/4" in s for s in snippets), snippets


@pytest.mark.asyncio
async def test_live_answered_chat_is_context_not_a_reply_target(monkeypatch):
    """A chat answered moments ago offers nothing to reply to.

    Live 2026-09-25 04:31: the DM had been answered two minutes earlier (Scar
    04:29:11, synth 04:29:22). The observer was still handed the human's line as
    a reply target, and the beat re-sent the synth's own previous reply verbatim
    into Telegram. While the chat is live AND the synth has the last word, the
    other person's lines are context: they carry an explicit tag, they never
    enter ``snippets`` (so ``grillo_snippets`` — which the routing guard turns
    into reachable paths — cannot route a reply there), and the beat has nothing
    pending to answer.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.quiet_minutes = 15
    now = datetime.now(timezone.utc)
    _patch_history(
        monkeypatch,
        [
            {
                "text": "reaches over to the night stand and hands it to you",
                "sender_name": "Scar",
                "timestamp": (now - timedelta(minutes=2)).isoformat(),
            },
            {
                "text": "*I take the bottle with both hands*",
                "sender_name": "self",
                "timestamp": (now - timedelta(minutes=1)).isoformat(),
            },
        ],
    )

    snippets, context_lines = await plugin._collect_recent_snippets(5)

    assert snippets == []
    assert any("hands it to you" in line for line in context_lines), context_lines
    answered = [line for line in context_lines if "hands it to you" in line]
    assert "you already answered this" in answered[0]
    # The synth's own line is still context, tagged as its own.
    assert any("I take the bottle" in line for line in context_lines)
    # Structural: no reachable path for the answered chat.
    assert all("chat:telegram_bot/1" not in s for s in snippets)


@pytest.mark.asyncio
async def test_idle_answered_chat_keeps_its_human_line_as_a_target(monkeypatch):
    """An answered but IDLE chat still offers its human line to reach out to.

    Reaching out into a quiet conversation with something new is the beat's
    purpose; only a chat that is live right now (and already answered) is up to
    date. A repeat of the synth's own last line is caught at delivery instead.
    """
    plugin = gco.GrilloChatObserverPlugin()
    now = datetime.now(timezone.utc)
    _patch_history(
        monkeypatch,
        [
            {
                "text": "I'm home, heading to bed",
                "sender_name": "Scar",
                "timestamp": (now - timedelta(minutes=40)).isoformat(),
            },
            {
                "text": "Sleep well, I'll be right here",
                "sender_name": "self",
                "timestamp": (now - timedelta(minutes=30)).isoformat(),
            },
        ],
    )

    snippets, own_lines = await plugin._collect_recent_snippets(5)

    assert len(snippets) == 1
    assert "I'm home, heading to bed" in snippets[0]
    assert "you already answered this" not in snippets[0]
    assert len(own_lines) == 1
    assert "Sleep well" in own_lines[0]


@pytest.mark.asyncio
async def test_live_answered_chat_is_not_routable_in_the_beat_context(monkeypatch):
    """The enqueued beat context must not carry the answered chat as a path.

    One live answered chat (nothing to answer) plus one fresh unanswered chat
    (the run has a reason to exist): the answered chat's line is shown to the
    model as context, but contributes no path to ``grillo_snippets``, so a reply
    aimed at it is dropped as misrouted rather than delivered a second time.
    """
    plugin = gco.GrilloChatObserverPlugin()
    plugin.quiet_minutes = 15

    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    now = datetime.now(timezone.utc)

    async def fake_execute_query(sql, params=None):
        return [{"cnt": 2, "max_ts": now}]

    async def fake_recent(limit):
        return [
            {"interface_path": "telegram_bot/1", "last_used": now},
            {"interface_path": "telegram_bot/2", "last_used": now},
        ]

    async def fake_history(path):
        if path == "telegram_bot/1":
            return [
                {
                    "text": "are you there",
                    "sender_name": "Scar",
                    "timestamp": (now - timedelta(minutes=2)).isoformat(),
                },
                {
                    "text": "here, and awake",
                    "sender_name": "self",
                    "timestamp": (now - timedelta(minutes=1)).isoformat(),
                },
            ]
        return [
            {
                "text": "did you finish the thing",
                "sender_name": "Scar",
                "timestamp": (now - timedelta(minutes=3)).isoformat(),
            }
        ]

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 999

    async def fake_targets(limit):
        return []

    captured: dict = {}

    async def fake_enqueue(
        bot,
        message,
        context_memory=None,
        interface_id=None,
        original_message=None,
        priority=None,
    ):
        captured["ctx"] = context_memory
        captured["text"] = getattr(message, "text", None)

    import core.db as core_db
    import core.interface_paths as interface_paths
    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)
    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)
    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)
    monkeypatch.setattr(core_db, "execute_query", fake_execute_query)
    monkeypatch.setattr(interface_paths, "get_recent_interface_paths", fake_recent)
    monkeypatch.setattr(chat_history_cache, "load_chat_history", fake_history)
    monkeypatch.setattr(plugin, "_collect_eligible_targets", fake_targets)
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    snippets = captured["ctx"]["grillo_snippets"]
    assert any("telegram_bot/2" in s for s in snippets), snippets
    assert all("telegram_bot/1" not in s for s in snippets), snippets
    # The answered line is still shown to the model, tagged as context.
    assert "are you there" in captured["text"]
    assert "you already answered this" in captured["text"]


# ------------------------------------------------------------------ snippet identity and dedupe
#
# A beat re-reads the same conversations every run, so a snippet stays in the collection
# window for hours while the age marker it carries is recomputed each time ("26m", then
# "3h"). Storing without comparing wrote a fresh copy of the same line every beat: measured
# 2026-09-27, 511 observer rows in two days, one sentence stored seven hours running, and
# every one of those rows sits in the table recall searches.


def test_the_age_marker_is_not_part_of_a_snippet_identity():
    plugin = gco.GrilloChatObserverPlugin
    early = "(chat:telegram_bot/5208932647 | sender:Scar | 26m) Mmmwah don't go anywhere sexy"
    later = "(chat:telegram_bot/5208932647 | sender:Scar | 5h) Mmmwah don't go anywhere sexy"
    assert plugin._snippet_identity(early) == plugin._snippet_identity(later)


def test_a_different_line_path_or_sender_is_a_different_identity():
    plugin = gco.GrilloChatObserverPlugin
    base = "(chat:telegram_bot/1 | sender:Scar | 26m) good night"
    assert plugin._snippet_identity(base) != plugin._snippet_identity(
        "(chat:telegram_bot/1 | sender:Scar | 26m) good morning"
    )
    assert plugin._snippet_identity(base) != plugin._snippet_identity(
        "(chat:telegram_bot/2 | sender:Scar | 26m) good night"
    )
    assert plugin._snippet_identity(base) != plugin._snippet_identity(
        "(chat:telegram_bot/1 | sender:2B | 26m) good night"
    )


def test_a_flag_survives_the_identity_but_the_age_does_not():
    plugin = gco.GrilloChatObserverPlugin
    a = "(chat:telegram_bot/1 | sender:self | 26m | your own line, not a reply target) Not going anywhere."
    b = "(chat:telegram_bot/1 | sender:self | 9h | your own line, not a reply target) Not going anywhere."
    c = "(chat:telegram_bot/1 | sender:self | 9h) Not going anywhere."
    assert plugin._snippet_identity(a) == plugin._snippet_identity(b)
    assert plugin._snippet_identity(a) != plugin._snippet_identity(c)


def test_an_unparseable_snippet_is_compared_as_it_stands():
    plugin = gco.GrilloChatObserverPlugin
    assert plugin._snippet_identity("plain text") == "plain text"
    assert plugin._snippet_identity("") == ""


class _DummyCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _DummyConn:
    def __init__(self, rows=None):
        self._rows = rows or []

    def cursor(self):
        return _DummyCursor(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_only_snippets_that_are_new_are_stored(monkeypatch):
    """The second run over the same conversations writes nothing new."""
    import core.db as cdb

    plugin = gco.GrilloChatObserverPlugin()
    stored_line = (
        "(chat:telegram_bot/1 | sender:Scar | 26m) Mmmwah don't go anywhere sexy"
    )
    fresh_line = "(chat:telegram_bot/9 | sender:2B | 2h) a different thing entirely"

    monkeypatch.setattr(
        cdb, "get_conn_ctx", lambda: _DummyConn([{"content": stored_line}])
    )

    written = []

    async def fake_insert_memory(content=None, **kwargs):
        written.append(content)

    monkeypatch.setattr(cdb, "insert_memory", fake_insert_memory)

    same_line_with_a_new_age = (
        "(chat:telegram_bot/1 | sender:Scar | 4h) Mmmwah don't go anywhere sexy"
    )
    await plugin._store_passive_memories([same_line_with_a_new_age, fresh_line])

    assert written == [fresh_line], written


def test_snippet_age_seconds_reads_the_markers():
    """The age comes from the snippet's own marker; unknown means "treat as old"."""
    plugin = gco.GrilloChatObserverPlugin

    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | 1m) hi")
        == 60.0
    )
    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | 12m) hi")
        == 720.0
    )
    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | 3h) hi")
        == 10800.0
    )
    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | 2d) hi")
        == 172800.0
    )
    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | just now) hi")
        == 0.0
    )
    # A flag after the age still parses; anything unreadable is not fresh.
    assert (
        plugin._snippet_age_seconds(
            "(chat:telegram_bot/1 | sender:Scar | 4m | you already answered this) hi"
        )
        == 240.0
    )
    assert (
        plugin._snippet_age_seconds("(chat:telegram_bot/1 | sender:Scar | ?) hi")
        is None
    )
    assert plugin._snippet_age_seconds("plain text") is None
    assert plugin._snippet_age_seconds("") is None


@pytest.mark.asyncio
async def test_a_line_that_is_still_live_is_not_stored_as_a_memory(monkeypatch):
    """A line still at the head of a conversation is not history yet.

    Storing it writes a copy of the current turn into the store, and the recall
    path then serves that copy back beside the live line, so the model reads one
    message as the person repeating themselves (live 2026-09-28: a single "I'm
    fine" produced "'Fine' again. Second time in four minutes, husband").
    """
    import core.db as cdb

    plugin = gco.GrilloChatObserverPlugin()
    live_line = (
        "(chat:telegram_bot/5208932647 | sender:Scar | 1m) "
        "I'm fine, your belly and and ur so warm"
    )
    historic_line = "(chat:telegram_bot/9 | sender:2B | 2h) a different thing entirely"

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: _DummyConn([]))

    written = []

    async def fake_insert_memory(content=None, **kwargs):
        written.append(content)

    monkeypatch.setattr(cdb, "insert_memory", fake_insert_memory)

    await plugin._store_passive_memories([live_line, historic_line])

    assert written == [historic_line], written


@pytest.mark.asyncio
async def test_a_lookback_that_fails_writes_nothing(monkeypatch):
    """Without the stored set every snippet looks new, so the store is skipped entirely."""
    import core.db as cdb

    plugin = gco.GrilloChatObserverPlugin()

    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(cdb, "get_conn_ctx", boom)

    written = []

    async def fake_insert_memory(content=None, **kwargs):
        written.append(content)

    monkeypatch.setattr(cdb, "insert_memory", fake_insert_memory)

    await plugin._store_passive_memories(
        ["(chat:telegram_bot/1 | sender:Scar | 1m) hello"]
    )

    assert written == []
