"""
plugins/grillo/grillo_self_reflection.py

Self-reflection prompt builder for G.R.I.L.L.O.
"""

from core.ai_plugin_base import AIPluginBase

display_name = "G.R.I.L.L.O Self Reflection"
BEAT_TYPE = "self_reflection"


class GrilloSelfReflectionPlugin(AIPluginBase):
    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict:
        return {}

    async def build_prompt(self) -> str:
        return (
            "[G.R.I.L.L.O. Self-Reflection]\n\n"
            "Check in with yourself: how are you feeling emotionally? What concerns or curiosities do you have?\n\n"
            "End with a JSON action to create a diary entry with your self-reflection. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`."
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "calm", "intensity": 0.6}], "content": "your reflection"}}]}'
        )


PLUGIN_CLASS = GrilloSelfReflectionPlugin
