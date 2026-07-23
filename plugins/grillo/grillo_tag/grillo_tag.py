"""
plugins/grillo/grillo_tag/grillo_tag.py

Tag Elaboration plugin for G.R.I.L.L.O.
Provides prompt-building for the 'tag_elaboration' beat.
"""

from core.ai_plugin_base import AIPluginBase

display_name = "G.R.I.L.L.O. Tag Elaboration"
BEAT_TYPE = "tag_elaboration"


class GrilloTagPlugin(AIPluginBase):
    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict:
        return {}

    async def build_prompt(self, tags=None) -> str:
        tags_text = "your recent conversations"
        if tags:
            tags_text = f"these topics: {', '.join(tags)}"
        prompt = (
            f"[G.R.I.L.L.O. Tag Elaboration]\n\n"
            f"Reflect on {tags_text}.\n\n"
            "Think about:\n"
            "- What patterns do you notice?\n"
            "- How do these themes connect?\n"
            "- What insights emerge?\n\n"
            "IMPORTANT: You MUST end your response with a JSON action to create a diary entry about this. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`. "
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "thoughtful", "intensity": 0.6}], "content": "your reflection"}}]}'
        )
        return prompt


PLUGIN_CLASS = GrilloTagPlugin
