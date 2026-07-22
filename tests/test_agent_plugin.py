from core.config_manager import config_registry
from plugins.agent_plugin import AgentPlugin


def test_is_enabled_reflects_toggle(monkeypatch):
    """The plugin must report its enabled state via is_enabled() so
    core_initializer skips registering the agent tools (and stops injecting
    them into prompts) when the agent is off.

    ``is_enabled()`` re-reads ``AGENT_ENABLED`` from the config registry on
    every call so WebUI toggles apply at runtime, so the test drives the
    toggle through the config lookup rather than the private attribute.
    """
    p = AgentPlugin()

    monkeypatch.setattr(config_registry, "get_var", lambda key, default=None: False)
    assert p.is_enabled() is False

    monkeypatch.setattr(config_registry, "get_var", lambda key, default=None: True)
    assert p.is_enabled() is True
