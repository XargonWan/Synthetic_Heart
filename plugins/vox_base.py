# plugins/vox_base.py
"""Base class for Vox TTS (text-to-speech / output) engines.

All Vox engines must subclass ``VoxEngineBase`` and implement at minimum
``generate_tts``.  The Vox core plugin handles the entire output pipeline:
writing the audio file, managing lip-sync, choosing the right interface
dispatcher, and fallback-to-text logic.  An engine only needs to produce bytes.

Register an engine at module import time:

    from core.vox_registry import register_vox_engine
    from plugins.vox_base import VoxEngineBase

    class MyTTSEngine(VoxEngineBase):
        display_name = "My TTS Engine"

        def generate_tts(self, text: str, **kwargs) -> bytes | None:
            ...

    ENGINE_CLASS = MyTTSEngine
    register_vox_engine("my_engine", __name__, label="My TTS engine description")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class VoxEngineBase(ABC):
    """Abstract base for all Vox TTS engines.

    The Vox core plugin calls ``generate_tts`` and then handles everything
    else (lip-sync, file I/O, dispatch, fallback, animation state, etc.).
    """

    display_name: str = "Unnamed Vox Engine"

    # ------------------------------------------------------------------
    # Required — audio generation
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Convert text to audio bytes.

        Args:
            text:    The text to synthesize.  The engine may receive pre-cleaned
                     text (stripped of emoji / markup) from the Vox plugin.
            emotion: Optional emotion/style hint (e.g. ``"happy"``, ``"sad"``).
            **kwargs: Engine-specific parameters forwarded from ``VOX_ENGINE_SETTINGS``.

        Returns:
            Raw audio bytes (WAV or raw PCM — see ``output_format``), or
            ``None`` if synthesis failed.
        """

    # ------------------------------------------------------------------
    # Optional — output format declaration
    # ------------------------------------------------------------------

    @property
    def output_format(self) -> str:
        """Audio format returned by ``generate_tts``.

        Accepted values:
          * ``"wav"``   – Audio already wrapped in RIFF/WAV headers (default).
          * ``"pcm"``   – Raw signed 16-bit little-endian PCM.  The Vox plugin
                          will wrap it in a WAV container using ``sample_rate``
                          and ``channels``.
        """
        return "wav"

    @property
    def sample_rate(self) -> int:
        """Sample rate of raw PCM output.  Ignored when ``output_format`` is ``"wav"``."""
        return 22050

    @property
    def channels(self) -> int:
        """Number of channels of raw PCM output.  Ignored when ``output_format`` is ``"wav"``."""
        return 1

    # ------------------------------------------------------------------
    # Optional — lip-sync data
    # ------------------------------------------------------------------

    def get_lipsync_data(self, audio_bytes: bytes) -> dict | None:
        """Optionally return phoneme/timing metadata usable by the WebUI lip-sync animator.

        If an engine can produce richer phoneme timings, it may return them here.
        The Vox plugin will attach this data to the ``synth:tts-play`` event.
        Returning ``None`` (default) causes the animator to fall back to
        amplitude-based estimation.
        """
        return None

    # ------------------------------------------------------------------
    # Optional — speaker metadata / samples
    # ------------------------------------------------------------------

    def get_speakers(self) -> list[dict]:
        """Return a list of available speaker definitions.

        Each entry should be a dict containing at least ``code`` and ``name``.
        Engines may also include a ``language`` field or other metadata that
        can be used by the UI.  The core Vox plugin will call this method when
        the WebUI requests ``/api/vox/speakers``; it is not required for
        all engines.

        Default implementation returns an empty list.
        """
        return []

    def sample(self, speaker: str) -> bytes:
        """Return a short WAV sample for the given speaker code.

        The WebUI will call the ``/api/vox/sample`` endpoint to fetch a
        preview when the user clicks "Listen".  Engines may cache or
        pre-generate the clip; if the requested speaker is not supported they
        should raise ``NotImplementedError`` or return ``b""``.
        """
        raise NotImplementedError("engine does not provide samples")

    # ------------------------------------------------------------------
    # Lifecycle hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the engine is loaded.  Download models, warm up, etc."""

    def teardown(self) -> None:
        """Called when the engine is unloaded or the application shuts down."""
