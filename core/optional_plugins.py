"""Guarded access to optional plugins from core and interface code.

Removing a plugin must never break the system (AGENTS.md), so core/interface
modules reach optional plugins through these helpers instead of importing them
directly: a missing or broken plugin degrades to "feature off".
"""

from __future__ import annotations


def is_vox_enabled() -> bool:
    """True when Vox is installed, enabled and has a working engine; else False.

    Interfaces use this to decide whether to offer ``send_as_voice``. It turns
    False for a short cooldown after every configured TTS engine (the active
    one and its fallbacks) has failed, so the Synth stops promising voice.
    """
    try:
        from plugins.vox_plugin import is_vox_available

        return bool(is_vox_available())
    except Exception:
        return False


VOICE_UNAVAILABLE_NOTE = (
    "Voice replies are temporarily unavailable (every text-to-speech engine "
    "failed), so everything is delivered as text only. If the user asks for "
    "audio or voice, tell them voice is not available right now."
)


def voice_unavailable_note() -> str:
    """A prompt note when Vox is on but all its engines recently failed, else ''."""
    try:
        from plugins.vox_plugin import is_vox_degraded

        return VOICE_UNAVAILABLE_NOTE if is_vox_degraded() else ""
    except Exception:
        return ""
