# plugins/iris_base.py
"""Base class for Iris vision (image/video understanding) engines.

Iris engines handle **file-based** vision analysis: given a path to an image
or video file they return a textual description.

All Iris engines must subclass ``IrisEngineBase`` and implement
``describe_image``.

Register an engine at module import time::

    from core.iris_registry import register_iris_engine
    from plugins.iris_base import IrisEngineBase, IrisResult

    class MyVisionEngine(IrisEngineBase):
        display_name = "My Vision Engine"

        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
        ) -> IrisResult | None:
            ...

    ENGINE_CLASS = MyVisionEngine
    register_iris_engine("my_engine", __name__, label="My vision engine description")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class IrisResult:
    """Result of an Iris vision analysis.

    Attributes:
        description: Textual description or analysis of the image/video.
        language:    BCP-47 / ISO-639-1 language code of the description
                     (e.g. ``"en"``, ``"it"``).  ``None`` when unknown.
        confidence:  Optional confidence score in [0.0, 1.0] from the engine.
        cached_path: Optional absolute path to a durable cached copy of the
                     analysed media, populated by the Iris subsystem so the
                     media can be re-inspected on later turns.  ``None`` when
                     no durable copy was kept.
    """

    description: str
    language: str | None = field(default=None)
    confidence: float | None = field(default=None)
    cached_path: str | None = field(default=None)


class IrisEngineBase(ABC):
    """Abstract base for all Iris vision engines.

    The Iris core plugin calls these methods and handles everything else
    (chain injection, session tracking, etc.).
    """

    display_name: str = "Unnamed Iris Engine"

    # ------------------------------------------------------------------
    # Required — file-based image/video analysis
    # ------------------------------------------------------------------

    @abstractmethod
    def describe_image(
        self,
        file_path: str,
        mime_type: str | None = None,
        prompt: str | None = None,
        model: str | None = None,
    ) -> IrisResult | None:
        """Analyse an image or video file and return a textual description.

        Args:
            file_path:  Absolute path to the image or video file on disk.
            mime_type:  Optional MIME hint, e.g. ``"image/jpeg"``, ``"image/png"``.
            prompt:     Optional free-text instruction for the engine, e.g.
                        ``"Describe what you see in detail."``
            model:      Optional model name override for vision analysis.

        Returns:
            :class:`IrisResult` containing the description and, when known, the
            language of the response.
            Returns ``None`` if analysis failed entirely.
        """

    # ------------------------------------------------------------------
    # Lifecycle hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the engine is loaded.  Download models, warm up, etc."""

    def teardown(self) -> None:
        """Called when the engine is unloaded or the application shuts down."""
