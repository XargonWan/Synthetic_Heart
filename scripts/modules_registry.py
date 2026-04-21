"""SyntH add-on module registry.

Modules are optional feature bundles installable on top of the core SyntH
installation.  Each ModuleSpec declares:

  uv_packages:   pip packages to add via ``uv add``
  env_vars:      .env key=value suggestions written after install
  post_install:  name of a function in module_installer.py to run after
                 package install (e.g. to trigger a model download)
  requires_restart: whether SyntH must be restarted to activate the module

Add new modules here — module_installer.py requires no changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    description: str
    uv_packages: list[str]
    env_vars: dict[str, str]
    post_install: str | None = None
    requires_restart: bool = True


MODULES: dict[str, ModuleSpec] = {
    # ── TTS engines ────────────────────────────────────────────────────────
    "tts-edge": ModuleSpec(
        name="Edge TTS",
        description="Microsoft Edge neural TTS — free, cloud-based, many voices.",
        uv_packages=["edge-tts>=6.1.9"],
        env_vars={"ACTIVE_VOX_ENGINE": "edge_tts"},
    ),
    "tts-kitten": ModuleSpec(
        name="KittenTTS",
        description="Local neural TTS (~150 MB model, 8 English voices, CPU-ready).",
        uv_packages=[],  # kittentts is already in core deps
        env_vars={"ACTIVE_VOX_ENGINE": "kitten"},
        post_install="download_kitten_model",
    ),
    "tts-gtts": ModuleSpec(
        name="Google TTS (gTTS)",
        description="Google Translate TTS — free, cloud, multilingual.",
        uv_packages=[],  # gtts is already in core deps
        env_vars={"ACTIVE_VOX_ENGINE": "gtts"},
    ),
    # ── STT engines ────────────────────────────────────────────────────────
    "stt-vosk": ModuleSpec(
        name="VOSK (local STT)",
        description="Offline speech-to-text (~50 MB model).  CPU-only, works without internet.",
        uv_packages=[],  # vosk is already in core deps
        env_vars={"ACTIVE_AURIS_ENGINE": "vosk"},
        post_install="download_vosk_model",
    ),
    "stt-whisper": ModuleSpec(
        name="Faster Whisper (local STT)",
        description="OpenAI Whisper via faster-whisper (CTranslate2).  Better accuracy than VOSK.",
        uv_packages=["faster-whisper>=0.8.1"],
        env_vars={"ACTIVE_AURIS_ENGINE": "whisper"},
    ),
    # ── Vision engines ─────────────────────────────────────────────────────
    "vision-gemini": ModuleSpec(
        name="Gemini Vision",
        description="Image/video understanding via Gemini API.  Requires GEMINI_API_KEY.",
        uv_packages=[],  # google-genai is already in core deps
        env_vars={"ACTIVE_IRIS_ENGINE": "gemini"},
    ),
}
