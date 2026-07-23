"""
plugins/grillo/grillo_curiosity/grillo_curiosity.py

Curiosity prompt builder for G.R.I.L.L.O.
"""

from core.ai_plugin_base import AIPluginBase

display_name = "G.R.I.L.L.O. Curiosity"
BEAT_TYPE = "curiosity"


class GrilloCuriosityPlugin(AIPluginBase):
    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict:
        return {}

    async def build_prompt(self, context_snippet: str | None = None) -> str:
        intro = "[G.R.I.L.L.O. Curiosity Exploration]\n\n"
        if context_snippet:
            intro += "Context:\n" + context_snippet + "\n\n"
        intro += (
            "What questions have emerged? What topics spark your curiosity?\n\n"
            "End with a JSON action to create a diary entry exploring your curiosity. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`."
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "curious", "intensity": 0.7}], "content": "your curious thoughts"}}]}'
        )
        return intro


PLUGIN_CLASS = GrilloCuriosityPlugin
