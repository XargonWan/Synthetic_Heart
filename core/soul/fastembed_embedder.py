"""ONNX-based text embedder for the SOUL pgvector backend.

Model is selected via the SOUL_EMBEDDER_ID env var (registry key from
scripts/embedders_registry.py).  Falls back to BAAI/bge-base-en-v1.5.

Execution provider order:
  CPU only (default):  CPUExecutionProvider
  GPU enabled:         DmlExecutionProvider → CUDAExecutionProvider → CPU
    Set SOUL_EMBEDDER_USE_GPU=1 in .env to opt in.
    DirectML (Dml) covers any Windows GPU (NVIDIA, AMD, Intel) without CUDA.
    CUDAExecutionProvider is the fallback for Linux NVIDIA.

The model is downloaded on first use (lazy via @cached_property) and cached
under SYNTH_MODELS_DIR/fastembed/ — same root as the core model_manager.
"""

from __future__ import annotations

import asyncio
import os
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_DEFAULT_MODEL_ID = "BAAI/bge-base-en-v1.5"


def _cache_dir() -> str:
    root = Path(os.environ.get("SYNTH_MODELS_DIR", Path.home() / ".cache/synth/models"))
    return str(root / "fastembed")


def _onnx_providers() -> list[str]:
    if os.getenv("SOUL_EMBEDDER_USE_GPU", "0") == "1":
        return ["DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


class FastEmbedder:
    """Async embedder wrapping fastembed.TextEmbedding (ONNX Runtime)."""

    def __init__(self, model_id: str = _DEFAULT_MODEL_ID) -> None:
        self._model_id = model_id

    @cached_property
    def _model(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(
            model_name=self._model_id,
            cache_dir=_cache_dir(),
            providers=_onnx_providers(),
        )

    async def embed(self, text: str) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    def _embed_sync(self, text: str) -> list[float]:
        results = list(self._model.embed([text]))
        return results[0].tolist()

    @property
    def model_id(self) -> str:
        return self._model_id
