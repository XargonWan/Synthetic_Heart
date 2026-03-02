# plugins/vox_engines/kitten.py
"""Vox TTS engine: Kitten — lightweight TTS via Silero V3 (CPU-capable).

Uses Silero TTS (https://github.com/snakers4/silero-models) loaded via
torch.hub.  The 54 MB English model is downloaded on first use and cached
in ``~/.cache/torch/hub``.

Requirements (already present in the SyntH venv):
    torch, torchaudio, soundfile, omegaconf

Registration is performed at import time.
"""

from __future__ import annotations

import io
import threading
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_error, log_info, log_warning
from core.variables_engine import register_exposed_var
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase

# ---------------------------------------------------------------------------
# Module-level model cache (loaded once, reused across calls)
# ---------------------------------------------------------------------------
_model: Any = None
_model_lock = threading.Lock()

_SILERO_LANG = "en"
_SILERO_SPEAKER_MODEL = "v3_en"
_SILERO_SPEAKER = "en_1"  # one of: en_0 … en_117
_SILERO_SAMPLE_RATE = 24000  # must be 8000 | 24000 | 48000

# Expose Kitten-specific settings in the WebUI → Components section.
register_exposed_var(
    "KITTEN_SPEAKER",
    label="Kitten TTS — Speaker",
    default="en_1",
    value_type=str,
    ui_type="select",
    options=[f"en_{i}" for i in range(118)],
    description="Silero V3 speaker ID (en_0 … en_117). Changes Kitten's voice.",
    scope="plugins",
    component="vox_plugin",
    advanced=False,
)

register_exposed_var(
    "KITTEN_SAMPLE_RATE",
    label="Kitten TTS — Sample Rate (Hz)",
    default=24000,
    value_type=int,
    ui_type="select",
    options=[8000, 24000, 48000],
    description="Audio sample rate in Hz. Must be 8000, 24000 or 48000.",
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)


def _load_model() -> Any:
    """Load (and cache) the Silero TTS model. Thread-safe."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            import torch

            log_info(
                "[vox/kitten] Loading Silero TTS model (first run may download ~55 MB)…"
            )
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language=_SILERO_LANG,
                speaker=_SILERO_SPEAKER_MODEL,
                trust_repo=True,
                verbose=False,
            )
            model.to(torch.device("cpu"))
            _model = model
            log_info("[vox/kitten] Silero TTS model ready.")
        except Exception as exc:
            log_error(f"[vox/kitten] Failed to load Silero model: {exc}")
    return _model


class KittenVoxEngine(VoxEngineBase):
    """Kitten engine — CPU-capable TTS via Silero V3."""

    display_name = "Kitten (Silero TTS)"

    @property
    def output_format(self) -> str:
        return "wav"

    def setup(self) -> None:
        try:
            import torch  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError as exc:
            log_warning(
                f"[vox/kitten] Missing dependency: {exc}. TTS will be unavailable."
            )
            return
        # Pre-load model in background so first TTS call is fast.
        t = threading.Thread(target=_load_model, daemon=True)
        t.start()

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        try:
            import soundfile as sf
        except ImportError:
            log_error("[vox/kitten] soundfile not installed.")
            return None

        model = _load_model()
        if model is None:
            log_error("[vox/kitten] Silero model not available.")
            return None

        # Allow caller to override speaker/sample_rate; fall back to exposed config vars.
        speaker = kwargs.get("speaker") or config_registry.get_value(
            "KITTEN_SPEAKER",
            _SILERO_SPEAKER,
            value_type=str,
            group="plugins",
            component="vox_plugin",
        )
        sample_rate = int(
            kwargs.get("sample_rate")
            or config_registry.get_value(
                "KITTEN_SAMPLE_RATE",
                _SILERO_SAMPLE_RATE,
                value_type=int,
                group="plugins",
                component="vox_plugin",
            )
        )
        if sample_rate not in (8000, 24000, 48000):
            log_warning(
                f"[vox/kitten] Unsupported sample_rate {sample_rate}; defaulting to 24000."
            )
            sample_rate = 24000

        try:
            audio = model.apply_tts(text=text, speaker=speaker, sample_rate=sample_rate)
            buf = io.BytesIO()
            sf.write(buf, audio.numpy(), sample_rate, format="WAV")
            return buf.getvalue()
        except Exception as exc:
            log_error(f"[vox/kitten] Synthesis failed: {exc}")
            return None


ENGINE_CLASS = KittenVoxEngine

register_vox_engine(
    name="kitten",
    module_path=__name__,
    capabilities={
        "voice_cloning": False,
        "emotions": False,
        "streaming": False,
        "local": True,
    },
    label="KittenTTS — lightweight TTS optimised for low-performance hardware.",
)
