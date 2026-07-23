"""
plugins/grillo/grillo_relationship/grillo_relationship.py

Relationship reflection prompt builder for G.R.I.L.L.O.
"""

from core.ai_plugin_base import AIPluginBase

display_name = "G.R.I.L.L.O. Relationship Reflection"
BEAT_TYPE = "relationship"


class GrilloRelationshipPlugin(AIPluginBase):
    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict:
        return {}

    async def build_prompt(self) -> str:
        return (
            "[G.R.I.L.L.O. Relationship Reflection]\n\n"
            "Reflect on recent interactions: how were your conversations, what patterns do you notice?\n\n"
            "End with a JSON action to create a diary entry about relationship insights. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`."
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "affection", "intensity": 0.6}], "content": "your insights"}}]}'
        )


PLUGIN_CLASS = GrilloRelationshipPlugin
