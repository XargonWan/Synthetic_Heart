"""Soul subsystem for memory and emotion architecture.

This package contains a standalone implementation of the SOUL rewrite
foundation so it can be integrated incrementally without destabilizing
current runtime paths.
"""

from .compiler import SoulCompiler
from .emotion_engine import EmotionalEngine
from .models import (
    DspExtraction,
    DspVersion,
    EmotionalEvent,
    EmotionalProfile,
    EmotionalState,
    EmotionalTag,
    ForesightSignal,
    KgTriple,
    MemCell,
    MemScene,
    compute_memcell_salience,
)
from .repository import InMemorySoulRepository, PostgresSoulRepository, SoulRepository

__all__ = [
    "DspExtraction",
    "DspVersion",
    "EmotionalEngine",
    "EmotionalEvent",
    "EmotionalProfile",
    "EmotionalState",
    "EmotionalTag",
    "ForesightSignal",
    "InMemorySoulRepository",
    "PostgresSoulRepository",
    "KgTriple",
    "MemCell",
    "MemScene",
    "SoulCompiler",
    "SoulRepository",
    "compute_memcell_salience",
]
