# plugins/auris_base.py
"""Base class for Auris STT (speech-to-text / input) engines.

Auris engines handle **file-based** transcription only: given a path to an
audio file they return a text string.  For real-time / bidirectional streaming
use ``LiveEngineBase`` (``plugins/live_base.py``) and the Live registry.

All Auris engines must subclass ``AurisEngineBase`` and implement
``transcribe``.

Register an engine at module import time:

    from core.auris_registry import register_auris_engine
    from plugins.auris_base import AurisEngineBase

    class MySTTEngine(AurisEngineBase):
        display_name = "My STT Engine"

        def transcribe(self, file_path: str, mime_type: str | None = None) -> str | None:
            ...

    ENGINE_CLASS = MySTTEngine
    register_auris_engine("my_engine", __name__, label="My STT engine description")
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AurisEngineBase(ABC):
    """Abstract base for all Auris STT engines.

    The Auris core plugin calls these methods and handles everything else
    (chain injection, VAD orchestration, session tracking, etc.).
    """

    display_name: str = "Unnamed Auris Engine"

    # ------------------------------------------------------------------
    # Required — file-based transcription
    # ------------------------------------------------------------------

    @abstractmethod
    def transcribe(self, file_path: str, mime_type: str | None = None) -> str | None:
        """Transcribe an audio/video file and return the text.

        Args:
            file_path:  Absolute path to the audio file on disk.
            mime_type:  Optional MIME hint, e.g. ``"audio/ogg"``, ``"audio/wav"``.

        Returns:
            Transcribed text string, or ``None`` if transcription failed.
        """

    # ------------------------------------------------------------------
    # Lifecycle hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the engine is loaded.  Download models, warm up, etc."""

    def teardown(self) -> None:
        """Called when the engine is unloaded or the application shuts down."""
