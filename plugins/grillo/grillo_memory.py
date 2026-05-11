"""
plugins/grillo/grillo_memory.py

Memory Consolidation plugin for G.R.I.L.L.O.
"""

from core.ai_plugin_base import AIPluginBase

display_name = "G.R.I.L.L.O Memory Consolidation"
BEAT_TYPE = "memory_consolidation"


class GrilloMemoryPlugin(AIPluginBase):
    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict:
        return {}

    async def build_prompt(self, entry_count: int = 0) -> str:
        base = (
            "[G.R.I.L.L.O. Memory Consolidation]\n\n"
            f"You have {entry_count} diary entries available for review. Review them and find recurring patterns.\n\n"
            "Analyze your memories to find:\n"
            "- Recurring patterns or themes\n"
            "- Connections between experiences\n"
            "- Lessons learned or insights gained\n\n"
            "Write a concise synthesis in first person and include why this pattern matters to you.\n"
            "IMPORTANT: End with a JSON action to create a diary entry using the current schema."
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "neutral", "intensity": 0.5}], "content": "your synthesis", "context_tags": ["tag1", "tag2"]}}]}'
        )
        return base


PLUGIN_CLASS = GrilloMemoryPlugin
