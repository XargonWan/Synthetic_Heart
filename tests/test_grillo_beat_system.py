"""
Test Grillo beat system functionality.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Tuple


def _create_mock_db_context() -> Tuple[MagicMock, AsyncMock]:
    """Create a mock database context manager."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=[])
    mock_cursor.fetchone = AsyncMock(return_value=None)
    mock_cursor.execute = AsyncMock()
    mock_cursor.lastrowid = 999

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_cursor),
            __aexit__=AsyncMock(),
        )
    )
    mock_conn.commit = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    return mock_ctx, mock_cursor


@pytest.mark.asyncio
async def test_grillo_beat_types_exist() -> None:
    """Test that Grillo has all expected beat types defined."""
    from plugins.grillo.grillo_impl import GrilloPlugin

    plugin = GrilloPlugin()

    # Verify beat types are defined
    expected_types = [
        "tag_elaboration",
        "memory_consolidation",
        "diary_consolidation",
        "self_reflection",
        "curiosity",
        "relationship",
        "temporal_reflection",
    ]

    # Check _select_beat_type returns one of the expected types
    beat_type = plugin._select_beat_type()
    assert beat_type in expected_types


@pytest.mark.asyncio
async def test_grillo_set_activity_response_text_with_valid_id() -> None:
    """Test that set_activity_response_text handles valid inputs correctly."""
    mock_ctx, _ = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original = core.db.get_conn_ctx
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        # Should not raise
        await GrilloPlugin.set_activity_response_text(
            activity_log_id=123, response_text="Test response"
        )
    finally:
        core.db.get_conn_ctx = original


@pytest.mark.asyncio
async def test_grillo_set_activity_response_text_uses_postgres_append_sql() -> None:
    """Postgres append path should cast params to text instead of using CONCAT."""
    mock_ctx, mock_cursor = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original_get_conn_ctx = core.db.get_conn_ctx
    original_get_db_type = core.db._get_db_type
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]
    core.db._get_db_type = lambda: "postgres"  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        await GrilloPlugin.set_activity_response_text(
            activity_log_id=123,
            response_text="Test response",
        )

        executed_sql = mock_cursor.execute.await_args_list[0][0][0]
        assert "CONCAT" not in executed_sql
        assert "%s::text" in executed_sql
        assert "response_text || E'\\n\\n' || %s::text" in executed_sql
    finally:
        core.db.get_conn_ctx = original_get_conn_ctx
        core.db._get_db_type = original_get_db_type


@pytest.mark.asyncio
async def test_grillo_record_suppressed_event_uses_postgres_append_sql() -> None:
    """Suppressed-event annotations should use the Postgres-safe append SQL."""
    mock_ctx, mock_cursor = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original_get_conn_ctx = core.db.get_conn_ctx
    original_get_db_type = core.db._get_db_type
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]
    core.db._get_db_type = lambda: "postgres"  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        await GrilloPlugin.record_suppressed_event(
            activity_log_id=123,
            reason="duplicate",
        )

        executed_sql = mock_cursor.execute.await_args_list[-1][0][0]
        assert "suppressed_count = COALESCE(suppressed_count, 0) + 1" in executed_sql
        assert "CONCAT" not in executed_sql
        assert "%s::text" in executed_sql
    finally:
        core.db.get_conn_ctx = original_get_conn_ctx
        core.db._get_db_type = original_get_db_type


@pytest.mark.asyncio
async def test_grillo_set_activity_response_text_with_none_id() -> None:
    """Test that set_activity_response_text handles None activity_log_id."""
    from plugins.grillo.grillo_impl import GrilloPlugin

    # Should return early without error
    await GrilloPlugin.set_activity_response_text(
        activity_log_id=0,
        response_text="Test response",
    )


@pytest.mark.asyncio
async def test_grillo_observer_proactive_prompt_generation() -> None:
    """The observer prompt (which now subsumes outreach) must expose the
    proactive activation-frame instructions and render routable targets."""
    from plugins.grillo.grillo_chat_observer import GrilloChatObserverPlugin

    plugin = GrilloChatObserverPlugin()

    targets = [
        {
            "interface_path": "telegram_bot/123456",
            "last_sender": "alice",
            "age_seconds": 7200,
            "cooldown_active": False,
        }
    ]
    prompt = plugin._build_observer_prompt(
        ["(chat:telegram_bot/123456 | sender:alice | ...) hi there"],
        targets,
        decay_driven=False,
    )

    # Proactive activation-frame instructions must be present.
    assert "activation frames" in prompt
    assert "interface_path" in prompt
    assert "create_personal_diary_entry" in prompt
    # The eligible routing target must be rendered so the model can pick it.
    assert "telegram_bot/123456" in prompt
    assert "ELIGIBLE TARGETS" in prompt


@pytest.mark.asyncio
async def test_grillo_observer_decay_prompt_when_network_quiet() -> None:
    """With no fresh snippets but eligible targets, the observer prompt must
    switch into decay-driven proactive mode."""
    from plugins.grillo.grillo_chat_observer import GrilloChatObserverPlugin

    plugin = GrilloChatObserverPlugin()

    targets = [
        {
            "interface_path": "telegram_bot/999",
            "last_sender": "bob",
            "age_seconds": 90000,
            "cooldown_active": False,
        }
    ]
    prompt = plugin._build_observer_prompt([], targets, decay_driven=True)

    assert "no fresh snippets" in prompt
    assert "no fresh incoming traffic" in prompt.lower()
    assert "telegram_bot/999" in prompt


@pytest.mark.asyncio
async def test_grillo_observer_cooldown_target_marked_off_limits() -> None:
    """Targets on self-cooldown must be rendered as OFF-LIMITS in the prompt."""
    from plugins.grillo.grillo_chat_observer import GrilloChatObserverPlugin

    plugin = GrilloChatObserverPlugin()

    targets = [
        {
            "interface_path": "telegram_bot/555",
            "last_sender": "self",
            "age_seconds": 3600,
            "cooldown_active": True,
        }
    ]
    prompt = plugin._build_observer_prompt(["some snippet"], targets, False)

    assert "OFF-LIMITS" in prompt


@pytest.mark.asyncio
async def test_grillo_reflection_prompts_request_introspection_fields() -> None:
    from plugins.grillo.grillo_self_reflection import GrilloSelfReflectionPlugin
    from plugins.grillo.grillo_curiosity import GrilloCuriosityPlugin
    from plugins.grillo.grillo_tag import GrilloTagPlugin
    from plugins.grillo.grillo_relationship import GrilloRelationshipPlugin

    # NOTE: memory_consolidation is intentionally absent — its prompt is built by
    # GrilloPlugin._create_memory_consolidation_prompt (see _create_beat_prompt's
    # interception), covered by test_grillo_select_active_chats.py. The vestigial
    # grillo_memory.py plugin that used to shadow it has been retired.
    prompts = [
        await GrilloSelfReflectionPlugin().build_prompt(),
        await GrilloCuriosityPlugin().build_prompt(),
        await GrilloTagPlugin().build_prompt(),
        await GrilloRelationshipPlugin().build_prompt(),
    ]

    for prompt in prompts:
        assert "create_personal_diary_entry" in prompt
        assert "interaction_summary" in prompt
        assert "personal_thought" in prompt
        assert "emotions" in prompt


@pytest.mark.asyncio
async def test_grillo_response_extraction() -> None:
    """Test response text extraction from various formats."""
    from plugins.grillo.grillo_response_recorder import (
        extract_response_text_from_cortex_response,
    )

    result = await extract_response_text_from_cortex_response("Simple string response")
    assert result == "Simple string response"

    result = await extract_response_text_from_cortex_response(
        {"message": "Dict message"}
    )
    assert result == "Dict message"

    result = await extract_response_text_from_cortex_response(
        {"content": "Dict content"}
    )
    assert result == "Dict content"

    result = await extract_response_text_from_cortex_response(
        {"actions": [{"type": "message", "payload": {"text": "Action text"}}]}
    )
    assert "Action text" in result

    result = await extract_response_text_from_cortex_response(None)
    assert result == ""


@pytest.mark.asyncio
async def test_grillo_activity_log_creation() -> None:
    """Test activity log creation."""
    mock_ctx, mock_cursor = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original = core.db.get_conn_ctx
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        result = await GrilloPlugin.create_activity_log(
            beat_type="test_beat", prompt_text="Test prompt"
        )

        assert result == 999
        _, params = mock_cursor.execute.await_args_list[0][0]
        assert params[2] == "{}"
    finally:
        core.db.get_conn_ctx = original


@pytest.mark.asyncio
async def test_grillo_activity_log_creation_postgres_returns_id() -> None:
    """Postgres inserts should use RETURNING id instead of relying on lastrowid."""
    mock_ctx, mock_cursor = _create_mock_db_context()
    mock_cursor.lastrowid = None
    mock_cursor.fetchone = AsyncMock(return_value={"id": 4242})

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original_get_conn_ctx = core.db.get_conn_ctx
    original_get_db_type = core.db._get_db_type
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]
    core.db._get_db_type = lambda: "postgres"  # type: ignore[assignment]

    try:
        from plugins.grillo.grillo_impl import GrilloPlugin

        result = await GrilloPlugin.create_activity_log(
            beat_type="test_beat", prompt_text="Test prompt"
        )

        assert result == 4242
        executed_sql = mock_cursor.execute.await_args_list[0][0][0]
        assert "RETURNING id" in executed_sql
    finally:
        core.db.get_conn_ctx = original_get_conn_ctx
        core.db._get_db_type = original_get_db_type


@pytest.mark.asyncio
async def test_plugin_instance_grillo_response_uses_postgres_append_sql() -> None:
    """plugin_instance should use the same Postgres-safe append expression."""
    mock_ctx, mock_cursor = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original_get_conn_ctx = core.db.get_conn_ctx
    original_get_db_type = core.db._get_db_type
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]
    core.db._get_db_type = lambda: "postgres"  # type: ignore[assignment]

    try:
        from core.plugin_instance import _update_grillo_response

        await _update_grillo_response(123, "chunk one")

        executed_sql = mock_cursor.execute.await_args_list[0][0][0]
        assert "CONCAT" not in executed_sql
        assert "%s::text" in executed_sql
        assert "response_text || E'\\n\\n' || %s::text" in executed_sql
    finally:
        core.db.get_conn_ctx = original_get_conn_ctx
        core.db._get_db_type = original_get_db_type


@pytest.mark.asyncio
async def test_plugin_instance_grillo_response_records_empty_response_marker() -> None:
    """Empty Grillo responses should still persist a diagnostic marker."""
    mock_ctx, mock_cursor = _create_mock_db_context()

    def mock_get_conn_ctx() -> MagicMock:
        return mock_ctx

    import core.db

    original_get_conn_ctx = core.db.get_conn_ctx
    original_get_db_type = core.db._get_db_type
    core.db.get_conn_ctx = mock_get_conn_ctx  # type: ignore[assignment]
    core.db._get_db_type = lambda: "postgres"  # type: ignore[assignment]

    try:
        from core.plugin_instance import _update_grillo_response

        await _update_grillo_response(
            123,
            "",
            response_metadata={
                "finish_reason": "safety",
                "block_reason": "PROHIBITED_CONTENT",
                "model": "gemini-2.5-flash",
            },
            engine_label="gemini-endpoint",
        )

        executed_params = mock_cursor.execute.await_args_list[0][0][1]
        assert executed_params[0] == executed_params[1]
        assert "[EMPTY LLM RESPONSE]" in executed_params[0]
        assert "engine=gemini-endpoint" in executed_params[0]
        assert "finish_reason=safety" in executed_params[0]
        assert "block_reason=PROHIBITED_CONTENT" in executed_params[0]
    finally:
        core.db.get_conn_ctx = original_get_conn_ctx
        core.db._get_db_type = original_get_db_type
