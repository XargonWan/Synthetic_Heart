"""Destination scoping for the shared ``send_message`` validation rules.

``send_message`` is ONE action name exposed by every chat interface, and the
validation registry keys rules by that name. Before scoping existed, a limit
declared by one interface gated every destination: Discord's 2000-character cap
rejected a 2.4k-character reply bound for Telegram (which accepts 4096), the
corrector retried, the model wrote another long reply, and the retry budget ran
out with nothing delivered (live incident 2026-09-17 10:29Z).

Rules may therefore declare ``applies_to_interface``; the registry skips them
when the payload is addressed somewhere else. The synth keeps full freedom to
choose the output interface — an explicit ``interface_path`` always decides,
even when it points away from the conversation the turn arrived on.
"""

from types import SimpleNamespace

from core.validation_registry import ValidationRegistry, ValidationRule


def _scoped_rule(component: str, scoped_to: str | None, message: str) -> ValidationRule:
    def _validator(payload):  # noqa: ARG001 - payload intentionally ignored
        return [message]

    return ValidationRule(
        action_type="send_message",
        custom_validator=_validator,
        component_name=component,
        applies_to_interface=scoped_to,
    )


def _registry_with_discord_and_telegram() -> ValidationRegistry:
    registry = ValidationRegistry()
    registry.register_component_rules(
        "discord_interface",
        [_scoped_rule("discord_interface", "discord_bot", "discord only")],
    )
    registry.register_component_rules(
        "telegram_bot", [_scoped_rule("telegram_bot", "telegram_bot", "telegram only")]
    )
    return registry


def test_rule_scoped_to_another_interface_is_skipped():
    registry = _registry_with_discord_and_telegram()

    errors = registry.validate_action_payload(
        "send_message", {"text": "hi"}, destination_interface="telegram_bot"
    )

    assert errors == ["telegram only"]


def test_rule_scoped_to_the_destination_applies():
    registry = _registry_with_discord_and_telegram()

    errors = registry.validate_action_payload(
        "send_message", {"text": "hi"}, destination_interface="discord_bot"
    )

    assert errors == ["discord only"]


def test_unscoped_rules_always_apply():
    registry = ValidationRegistry()
    registry.register_component_rules("core", [_scoped_rule("core", None, "always")])

    for destination in ("discord_bot", "telegram_bot", None):
        assert registry.validate_action_payload(
            "send_message", {"text": "hi"}, destination_interface=destination
        ) == ["always"]


def test_unknown_destination_applies_every_rule():
    """Fail-safe: an undetermined destination must never silently under-validate."""
    registry = _registry_with_discord_and_telegram()

    errors = registry.validate_action_payload("send_message", {"text": "hi"})

    assert sorted(errors) == ["discord only", "telegram only"]


# ---------------------------------------------------------------------------
# Destination resolution (mirrors _dispatch_send_message)
# ---------------------------------------------------------------------------


def test_resolve_destination_prefers_the_explicit_interface_path():
    from core.action_parser import _resolve_validation_destination

    payload = {"interface_path": "telegram_bot/5208932647"}
    origin = SimpleNamespace(interface_path="discord_bot/1/2")

    # The synth chose Telegram: Telegram's rules govern, not the origin's.
    assert _resolve_validation_destination(payload, origin) == "telegram_bot"


def test_resolve_destination_falls_back_to_the_origin_conversation():
    from core.action_parser import _resolve_validation_destination

    origin = SimpleNamespace(interface_path="telegram_bot/5208932647")

    assert _resolve_validation_destination({}, origin) == "telegram_bot"


def test_resolve_destination_is_none_when_undetermined():
    from core.action_parser import _resolve_validation_destination

    assert _resolve_validation_destination({}, None) is None
    assert _resolve_validation_destination({"text": "hi"}, SimpleNamespace()) is None
    assert _resolve_validation_destination({"interface_path": "   "}, None) is None


def test_resolve_destination_handles_an_unregistered_path_prefix():
    from core.action_parser import _resolve_validation_destination

    assert (
        _resolve_validation_destination(
            {"interface_path": "some_new_interface/42"}, None
        )
        == "some_new_interface"
    )


# ---------------------------------------------------------------------------
# The real interfaces' rules are scoped
# ---------------------------------------------------------------------------


def test_discord_rule_does_not_gate_a_telegram_bound_payload():
    """The live failure: Discord's checks must not fire for Telegram traffic."""
    from core.action_parser import validate_action
    from interface.discord_interface import DiscordInterface

    DiscordInterface(bot_token="")  # construction registers Discord's rule

    _ok, errors = validate_action(
        {
            "type": "send_message",
            "payload": {"text": "   ", "interface_path": "telegram_bot/5208932647"},
        }
    )

    assert not any("only whitespace" in e for e in errors), errors


def test_discord_rule_still_gates_a_discord_bound_payload():
    from core.action_parser import validate_action
    from interface.discord_interface import DiscordInterface

    DiscordInterface(bot_token="")

    _ok, errors = validate_action(
        {
            "type": "send_message",
            "payload": {"text": "   ", "interface_path": "discord_bot/1/2"},
        }
    )

    # Applied to its own destination, and exactly once: re-registering a
    # component must not duplicate its rule in the correction prompt.
    assert [e for e in errors if "only whitespace" in e] == [
        "Message text cannot be empty or only whitespace"
    ], errors


def test_long_telegram_bound_payload_is_not_rejected():
    """Regression: a 2.5k-character reply addressed to Telegram must validate.

    It used to be rejected by Discord's 2000-character cap, and after the
    correction retries ran out the user got nothing.
    """
    from core.action_parser import validate_action
    from interface.discord_interface import DiscordInterface

    DiscordInterface(bot_token="")

    _ok, errors = validate_action(
        {
            "type": "send_message",
            "payload": {
                "text": "x" * 2500,
                "interface_path": "telegram_bot/5208932647",
            },
        }
    )

    assert not any("2000" in e for e in errors), errors
