"""Tests for the Recon Agent Intent plugin's attachment-aware instruction."""

from plugins.recon.recon_agent_intent import ReconAgentIntentPlugin


def test_base_instruction_has_no_attachment_note():
    plugin = ReconAgentIntentPlugin()
    instruction = plugin.get_recon_instruction()
    assert "attached" not in instruction
    assert "agent_needed" in instruction


def test_instruction_notes_attachments_when_present():
    plugin = ReconAgentIntentPlugin()
    instruction = plugin.get_recon_instruction(
        context_memory={"attachment_paths": ["/app/attachments/ideas.md"]}
    )
    assert "attached file" in instruction
    assert "does NOT require tools" in instruction


def test_instruction_stays_plain_without_attachments():
    plugin = ReconAgentIntentPlugin()
    for context_memory in (None, {}, {"attachment_paths": []}):
        instruction = plugin.get_recon_instruction(context_memory=context_memory)
        assert "attached file" not in instruction, context_memory


def test_web_search_delivery_turn_is_not_judged_agentic():
    import asyncio

    plugin = ReconAgentIntentPlugin()
    verdict = {"agent_needed": True, "reason": "multi-step", "task_title": "x"}

    delivery = {"beat_type": "web_search_result"}
    out = asyncio.run(
        plugin.parse_recon_response(verdict, context_memory=delivery, text="results")
    )
    assert out == [] and "agent_needed" not in delivery

    ordinary: dict = {}
    out = asyncio.run(
        plugin.parse_recon_response(verdict, context_memory=ordinary, text="do it")
    )
    assert ordinary.get("agent_needed") is True and out
