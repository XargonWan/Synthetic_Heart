"""Embedder model registry for SOUL pgvector backend.

Each EmbedderSpec declares:
  - model_id:       fastembed model name passed to TextEmbedding()
  - dims:           output vector dimensions — must match VECTOR(N) in the postgres schema
  - size_mb:        approximate download size shown to the user during setup
  - multilingual:   True if the model handles non-English text well
  - cpu_optimized:  True = fast on CPU; False = GPU strongly recommended
  - description:    one-line blurb shown in the setup wizard

SOUL_EMBEDDER_ID env var selects the active model at runtime.
The default is "bge-base-en" (English, 768-dim, fast CPU).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbedderSpec:
    model_id: str
    display_name: str
    dims: int
    size_mb: int
    multilingual: bool
    cpu_optimized: bool
    description: str


EMBEDDERS: dict[str, EmbedderSpec] = {
    "bge-base-en": EmbedderSpec(
        model_id="BAAI/bge-base-en-v1.5",
        display_name="BGE Base EN v1.5",
        dims=768,
        size_mb=108,
        multilingual=False,
        cpu_optimized=True,
        description="English-only, 768-dim, ~108 MB. Fast on CPU. Default choice.",
    ),
    "multilingual-e5-small": EmbedderSpec(
        model_id="intfloat/multilingual-e5-small",
        display_name="Multilingual E5 Small",
        dims=384,
        size_mb=117,
        multilingual=True,
        cpu_optimized=True,
        description="100 languages, 384-dim, ~117 MB. Lightest option — ideal for Raspberry Pi or low-RAM.",
    ),
    "multilingual-e5-base": EmbedderSpec(
        model_id="intfloat/multilingual-e5-base",
        display_name="Multilingual E5 Base",
        dims=768,
        size_mb=278,
        multilingual=True,
        cpu_optimized=True,
        description="100 languages, 768-dim, ~278 MB. Better quality multilingual, mid-range hardware.",
    ),
    "gemma-embedding": EmbedderSpec(
        model_id="google/gemma-embedding-exp-03-07",
        display_name="Gemma Embedding (Experimental)",
        dims=3072,
        size_mb=1600,
        multilingual=True,
        cpu_optimized=False,
        description="Google Gemma-arch, 3072-dim, ~1.6 GB. High quality but GPU strongly recommended.",
    ),
}

DEFAULT_EMBEDDER_KEY = "bge-base-en"


def get_spec(key: str) -> EmbedderSpec:
    """Return EmbedderSpec by registry key, falling back to the default."""
    return EMBEDDERS.get(key, EMBEDDERS[DEFAULT_EMBEDDER_KEY])


def get_spec_by_model_id(model_id: str) -> EmbedderSpec | None:
    """Look up a spec by its fastembed model_id string."""
    for spec in EMBEDDERS.values():
        if spec.model_id == model_id:
            return spec
    return None
