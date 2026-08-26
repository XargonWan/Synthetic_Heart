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
