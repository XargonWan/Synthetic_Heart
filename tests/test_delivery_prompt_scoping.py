"""Tests for the delivery-turn structural scoping fix (search-loop fix,
2026-08-17).

A delivery turn is enqueued as a JSON string (``{system_message,
allowed_action_types}``) which ``plugin_instance`` parses to a dict that
BYPASSES ``build_prompt_request``.  Before this fix the action catalog was
never trimmed for the weak model, so it re-emitted the producing action
(e.g. ``search_current_knowledge``) in a loop.  ``_scope_delivery_prompt_request``
rebuilds such a delivery turn as a ``PromptRequest`` whose tool declarations
are ``message_*`` only, hiding the producing action from the model.
"""

import pytest
from types import SimpleNamespace


def _delivery_dict(action_type="search_current_knowledge", with_outputs=True):
    """The parsed delivery dict exactly as auto_response enqueues it."""
    sm = {
        "type": "output",
        "action_type": action_type,
        "instruction": "IMPORTANT: These are the results from your action.",
        "message": "DELIVERY TASK: ...",
        "full_json_instructions": "{}",
        "is_action_result_delivery": True,
        "max_correction_attempts": 2,
    }
    if with_outputs:
        sm["action_outputs"] = [{"text": "search result one"}]
    return {"system_message": sm, "allowed_action_types": ["message_telegram_bot"]}


def _patch_core(monkeypatch):
    """Stub the build_delivery_request collaborators (persona gather + catalog)."""
    from core.core_initializer import core_initializer

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections",
        SimpleNamespace(__call__=lambda *a, **k: {}),
    )
    original_actions_block = getattr(core_initializer, "actions_block", None)
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
                "message_telegram_bot": {
                    "schema": {"type": "object", "properties": {}, "required": []},
                    "brief": "Send Telegram message.",
                    "source": "telegram_bot",
                },
            }
        },
        raising=False,
    )
    return original_actions_block


def _tool_names(prompt_request):
    return [
        str(getattr(m, "name", ""))
        for m in (getattr(prompt_request, "tool_declarations", None) or [])
    ]


@pytest.mark.asyncio
async def test_delivery_turn_scopes_to_message_only(monkeypatch):
    """A delivery turn yields a PromptRequest exposing only message_* tools."""
    from core.plugin_instance import _scope_delivery_prompt_request

    _patch_core(monkeypatch)
    result = await _scope_delivery_prompt_request(
        _delivery_dict(), "telegram_bot", "telegram_bot/1"
    )

    assert result is not None
    names = _tool_names(result)
    # The producing action must be hidden; only the message action remains.
    assert "search_current_knowledge" not in names
    assert names == ["message_telegram_bot"]
    # Delivery framing is set so the renderer expects a JSON-in-content reply.
    assert getattr(result, "mode", None) == "delivery"
    assert getattr(result, "supports_tool_calling", None) is False


@pytest.mark.asyncio
async def test_non_delivery_dict_returns_none(monkeypatch):
    """Ordinary (non-delivery) prompt dicts are left untouched."""
    from core.plugin_instance import _scope_delivery_prompt_request

    _patch_core(monkeypatch)
    assert (
        await _scope_delivery_prompt_request(
            {"input": {"payload": {"text": "hi"}}}, "telegram_bot", None
        )
        is None
    )


@pytest.mark.asyncio
async def test_delivery_without_action_outputs_returns_none(monkeypatch):
    """A delivery turn with no action_outputs (plain output) keeps the legacy path."""
    from core.plugin_instance import _scope_delivery_prompt_request

    _patch_core(monkeypatch)
    assert (
        await _scope_delivery_prompt_request(
            _delivery_dict(with_outputs=False), "telegram_bot", None
        )
        is None
    )


@pytest.mark.asyncio
async def test_non_dict_input_returns_none(monkeypatch):
    """String/other prompt inputs are never treated as a delivery turn."""
    from core.plugin_instance import _scope_delivery_prompt_request

    assert await _scope_delivery_prompt_request("not a dict", None, None) is None
    assert await _scope_delivery_prompt_request(None, None, None) is None
