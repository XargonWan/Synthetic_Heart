import pytest

from plugins.recon.recon_agent_intent import ReconAgentIntentPlugin


class Msg:
    interface_path = "telegram_bot/12345"


def _enable(monkeypatch, *, plugin_enabled=True, routing_on=True, agent_on=True):
    def fake_get_value(key, default=None, value_type=None):
        if key == "RECON_AGENT_INTENT_RECON_ENABLED":
            return plugin_enabled
        return default

    def fake_get_var(key, default=None, value_type=None):
        if key == "AGENTIC_ROUTING_ENABLED":
            return routing_on
        if key == "AGENT_ENABLED":
            return agent_on
        return default

    monkeypatch.setattr("core.config_manager.config_registry.get_value", fake_get_value)
    monkeypatch.setattr("core.config_manager.config_registry.get_var", fake_get_var)


@pytest.mark.asyncio
async def test_emits_instruction_when_agent_needed(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        {"agent_needed": True, "reason": "inspect the codebase"},
        message=Msg(),
        text="check your code and plan a feature",
    )
    assert len(contribs) == 1
    assert contribs[0]["type"] == "instruction"
    assert contribs[0]["source"] == "agent_intent"
    assert "agentic tool" in contribs[0]["content"]
    assert "inspect the codebase" in contribs[0]["content"]


@pytest.mark.asyncio
async def test_context_flag_set_when_agent_needed(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    ctx: dict = {}
    await plugin.parse_recon_response(
        {"agent_needed": True, "reason": "inspect the codebase"},
        message=Msg(),
        context_memory=ctx,
        text="check your code",
    )
    assert ctx.get("agent_needed") is True


@pytest.mark.asyncio
async def test_legacy_needs_agent_field_still_read(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    ctx: dict = {}
    contribs = await plugin.parse_recon_response(
        {"needs_agent": True, "reason": "legacy field"},
        message=Msg(),
        context_memory=ctx,
        text="check your code",
    )
    assert len(contribs) == 1
    assert ctx.get("agent_needed") is True


@pytest.mark.asyncio
async def test_task_title_written_to_context(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    ctx: dict = {}
    contribs = await plugin.parse_recon_response(
        {
            "agent_needed": True,
            "reason": "inspect the codebase",
            "task_title": "Analizza il codice",
        },
        message=Msg(),
        context_memory=ctx,
        text="check your code",
    )
    assert len(contribs) == 1
    assert ctx.get("agent_task_title") == "Analizza il codice"


@pytest.mark.asyncio
async def test_no_task_title_when_missing(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    ctx: dict = {}
    await plugin.parse_recon_response(
        {"agent_needed": True, "reason": "inspect the codebase"},
        message=Msg(),
        context_memory=ctx,
        text="check your code",
    )
    assert "agent_task_title" not in ctx


@pytest.mark.asyncio
async def test_no_instruction_when_agent_not_needed(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        {"agent_needed": False, "reason": "just a greeting"},
        message=Msg(),
        text="hi there",
    )
    assert contribs == []


@pytest.mark.asyncio
async def test_no_instruction_when_agent_toggle_off(monkeypatch):
    _enable(monkeypatch, agent_on=False)
    plugin = ReconAgentIntentPlugin()
    ctx: dict = {}
    contribs = await plugin.parse_recon_response(
        {"agent_needed": True, "reason": "inspect the codebase"},
        message=Msg(),
        context_memory=ctx,
        text="check your code",
    )
    assert contribs == []
    assert "agent_needed" not in ctx


@pytest.mark.asyncio
async def test_no_instruction_when_routing_off(monkeypatch):
    _enable(monkeypatch, routing_on=False)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        {"agent_needed": True, "reason": "inspect the codebase"},
        message=Msg(),
        text="check your code",
    )
    assert contribs == []


@pytest.mark.asyncio
async def test_no_instruction_when_plugin_disabled(monkeypatch):
    _enable(monkeypatch, plugin_enabled=False)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        {"agent_needed": True},
        message=Msg(),
        text="check your code",
    )
    assert contribs == []


@pytest.mark.asyncio
async def test_no_instruction_when_text_empty(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        {"agent_needed": True},
        message=Msg(),
        text="   ",
    )
    assert contribs == []


@pytest.mark.asyncio
async def test_no_instruction_when_data_not_dict(monkeypatch):
    _enable(monkeypatch)
    plugin = ReconAgentIntentPlugin()
    contribs = await plugin.parse_recon_response(
        "not-a-dict",
        message=Msg(),
        text="check your code",
    )
    assert contribs == []


def test_recon_key_and_instruction():
    plugin = ReconAgentIntentPlugin()
    assert plugin.get_recon_key() == "agent_intent"
    instr = plugin.get_recon_instruction()
    assert "agent_needed" in instr
    assert "never on specific words" in instr
    assert "task_title" in instr
