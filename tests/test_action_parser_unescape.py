import pytest

from core.action_parser import _maybe_unescape_text_in_payload, _handle_plugin_action


def test_maybe_unescape_text_in_payload_decodes_double_escaped():
    # Use double-escaped sequences so the python literal contains the backslashes
    payload = {
        "text": "Oh, Jay! Che bella domanda \\u2728\\n\\nHai perfettamente ragione"
    }
    # initial contains literal backslashes, not real newline or emoji
    assert "\\n\\n" in payload["text"]
    # Literal \u2728 sequence should be present in the original string
    assert "\\u2728" in payload["text"]

    _maybe_unescape_text_in_payload(payload)

    assert "\n\n" in payload["text"]
    # After unescape, the codepoint should appear as actual emoji
    assert "✨" in payload["text"]


@pytest.mark.asyncio
async def test_handle_plugin_action_forwards_unescaped_text_to_interface(monkeypatch):
    # Create a fake interface and register it
    class FakeInterface:
        def __init__(self):
            self.calls = []

        async def send_message(self, payload, original_message=None):
            self.calls.append(payload)

    fake = FakeInterface()

    # Import registry and set our fake
    import core.core_initializer as core_init

    # Ensure interface registry exists and set our fake under 'test_iface'
    core_init.INTERFACE_REGISTRY["test_iface"] = fake

    action = {
        "type": "message_test_iface",
        "interface": "test_iface",
        "payload": {
            "text": "Hello \\u2728\\n\\nWorld",
            "interface_path": "test_iface/1/0",
        },
    }

    # Call handler
    await _handle_plugin_action(action, context={}, bot=None, original_message=None)

    # Validate that fake interface received unescaped text
    assert len(fake.calls) == 1
    received = fake.calls[0]["text"]
    assert "\n\n" in received
    assert "✨" in received


# ---------------------------------------------------------------------------
# Delivery-outcome contract (Bug #1 regression tests)
#
# _handle_plugin_action must propagate the real delivery outcome as
# {"ok": bool, ...} so the agent tool executor / router can observe
# success vs failure instead of masking every send as success.
# Two dispatch branches are covered:
#   A) interface-registry branch  (no plugin found for the action)
#   B) plugin branch              (interface registered as a plugin) — the
#                                  path actually used in production.
# ---------------------------------------------------------------------------


def _make_action(iface: str, text: str = "hi") -> dict:
    return {
        "type": f"message_{iface}",
        "interface": iface,
        "payload": {"text": text, "interface_path": f"{iface}/1/0"},
    }


@pytest.mark.asyncio
async def test_registry_branch_reports_ok_on_success(monkeypatch):
    """Interface-registry branch: successful send -> {'ok': True}."""
    import core.action_parser as ap
    import core.core_initializer as core_init

    class OkInterface:
        async def send_message(self, payload, original_message=None):
            return True

    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [])
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, "reg_ok_iface", OkInterface())

    result = await _handle_plugin_action(
        _make_action("reg_ok_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_registry_branch_reports_failure_when_send_returns_falsy(monkeypatch):
    """Interface-registry branch: send returns None -> {'ok': False}."""
    import core.action_parser as ap
    import core.core_initializer as core_init

    class FailInterface:
        async def send_message(self, payload, original_message=None):
            return None  # non-delivery must NOT be masked as success

    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [])
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, "reg_fail_iface", FailInterface())

    result = await _handle_plugin_action(
        _make_action("reg_fail_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False


@pytest.mark.asyncio
async def test_registry_branch_reports_failure_on_exception(monkeypatch):
    """Interface-registry branch: send raises -> {'ok': False, 'error': ...}."""
    import core.action_parser as ap
    import core.core_initializer as core_init

    class RaiseInterface:
        async def send_message(self, payload, original_message=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [])
    monkeypatch.setitem(
        core_init.INTERFACE_REGISTRY, "reg_raise_iface", RaiseInterface()
    )

    result = await _handle_plugin_action(
        _make_action("reg_raise_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False
    assert "error" in result


def _make_plugin(iface: str, behavior):
    class FakePlugin:
        @classmethod
        def get_interface_id(cls):
            return iface

        async def send_message(self, payload, original_message=None):
            return await behavior(payload)

    return FakePlugin()


@pytest.mark.asyncio
async def test_plugin_branch_reports_ok_on_success(monkeypatch):
    """Plugin branch (production path): successful send -> {'ok': True}."""
    import core.action_parser as ap

    async def _ok(_payload):
        return True

    plugin = _make_plugin("plug_ok_iface", _ok)
    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [plugin])

    result = await _handle_plugin_action(
        _make_action("plug_ok_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_plugin_branch_reports_failure_when_send_returns_falsy(monkeypatch):
    """Plugin branch: send returns None -> {'ok': False} (was silently masked)."""
    import core.action_parser as ap

    async def _fail(_payload):
        return None

    plugin = _make_plugin("plug_fail_iface", _fail)
    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [plugin])

    result = await _handle_plugin_action(
        _make_action("plug_fail_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False


@pytest.mark.asyncio
async def test_plugin_branch_reports_failure_on_exception(monkeypatch):
    """Plugin branch: send raises -> {'ok': False, 'error': ...} (no fall-through)."""
    import core.action_parser as ap

    async def _raise(_payload):
        raise RuntimeError("boom")

    plugin = _make_plugin("plug_raise_iface", _raise)
    monkeypatch.setattr(ap, "_plugins_for", lambda _at: [plugin])

    result = await _handle_plugin_action(
        _make_action("plug_raise_iface"), context={}, bot=None, original_message=None
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False
    assert "error" in result
