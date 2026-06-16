"""Base module for Cortex External Engines (LLM providers + multi-capability).

Each engine module under this directory may declare:

    PLUGIN_CLASS = MyLLMPlugin          # required for LLM / cortex registration
    ENGINE_CLASS = MySTTAdapter         # required for STT / auris registration
    VOX_ENGINE_CLASS = MyTTSAdapter     # required for TTS / vox registration

    ENGINE_CAPABILITIES = {             # optional — defaults to {"llm": True}
        "llm": True,   # register in CortexRegistry as llm_provider engine
        "stt": False,  # register in AurisRegistry (requires ENGINE_CLASS)
        "tts": False,  # register in VoxRegistry   (requires VOX_ENGINE_CLASS)
    }

    # Optional per-capability metadata:
    AURIS_CAPABILITIES = {"file_based": True, "local": False}
    AURIS_ENGINE_LABEL = "Human-readable STT description"
    VOX_CAPABILITIES   = {}
    VOX_ENGINE_LABEL   = "Human-readable TTS description"

ENGINE_KIND is kept as ``"llm_provider"`` for backward-compatibility with
existing database rows that reference this string (e.g. ``BASE_CORTEX``
entries). The directory and code are renamed to ``external_engines`` but the
logical cortex kind string is unchanged.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Optional, cast

from core.auris_registry import AurisCapabilities
from core.logging_utils import log_debug, log_info, log_warning
from core.vox_registry import VoxCapabilities
from core.cortex_registry import Capabilities

ENGINE_KIND = "llm_provider"
ENGINE_LABEL = "LLM Provider engines (API / SDK based)"
CAPABILITIES: Optional[Capabilities] = None

_LOG_PREFIX = "[external_engines_base]"


def discover_and_register(registry: object, dev_enabled: bool = False) -> None:
    """Register the LLM Provider cortex kind and discover child engines.

    For each engine module the function reads ``ENGINE_CAPABILITIES`` to decide
    which registries (cortex, auris, vox) to register the engine with.
    """
    registry.register_cortex_kind(ENGINE_KIND, ENGINE_LABEL, CAPABILITIES)  # type: ignore[attr-defined]

    base_path = os.path.dirname(__file__)

    def _register_from_path(path: str, module_prefix: str) -> None:
        for _importer, module_name, is_pkg in pkgutil.iter_modules([path]):
            if is_pkg or module_name.startswith("_") or module_name.endswith("_base"):
                continue
            module_path = f"{module_prefix}.{module_name}"
            try:
                mod = importlib.import_module(module_path)
            except Exception as exc:
                log_warning(f"{_LOG_PREFIX} Failed to import {module_path}: {exc}")
                continue

            caps: dict[str, bool] = getattr(mod, "ENGINE_CAPABILITIES", {"llm": True})

            # --- LLM / CortexRegistry ---
            if caps.get("llm", True) and hasattr(mod, "PLUGIN_CLASS"):
                label: str | None = getattr(mod, "ENGINE_LABEL", None)
                if not label:
                    label = getattr(mod.PLUGIN_CLASS, "engine_label", None)
                registry.register_engine_module(  # type: ignore[attr-defined]
                    module_name,
                    module_path,
                    cortex=ENGINE_KIND,
                    label=label or None,
                )
                log_debug(f"{_LOG_PREFIX} LLM engine registered: {module_name}")

            # --- STT / AurisRegistry ---
            if caps.get("stt", False) and hasattr(mod, "ENGINE_CLASS"):
                try:
                    from core.auris_registry import register_auris_engine

                    auris_caps = getattr(
                        mod,
                        "AURIS_CAPABILITIES",
                        {"file_based": True, "local": False},
                    )
                    auris_label: str = getattr(
                        mod,
                        "AURIS_ENGINE_LABEL",
                        getattr(mod, "ENGINE_LABEL", "") or "",
                    )
                    register_auris_engine(
                        module_name,
                        module_path,
                        capabilities=cast(AurisCapabilities, auris_caps),
                        label=auris_label,
                    )
                    log_info(
                        f"{_LOG_PREFIX} STT (auris) engine registered: {module_name}"
                    )
                except Exception as exc:
                    log_warning(
                        f"{_LOG_PREFIX} Failed to register STT engine {module_name}: {exc}"
                    )

            # --- TTS / VoxRegistry ---
            if caps.get("tts", False) and hasattr(mod, "VOX_ENGINE_CLASS"):
                try:
                    from core.vox_registry import register_vox_engine

                    vox_caps = getattr(mod, "VOX_CAPABILITIES", {})
                    vox_label: str = getattr(
                        mod,
                        "VOX_ENGINE_LABEL",
                        getattr(mod, "ENGINE_LABEL", "") or "",
                    )
                    register_vox_engine(
                        module_name,
                        module_path,
                        capabilities=cast(VoxCapabilities, vox_caps),
                        label=vox_label,
                    )
                    log_info(
                        f"{_LOG_PREFIX} TTS (vox) engine registered: {module_name}"
                    )
                except Exception as exc:
                    log_warning(
                        f"{_LOG_PREFIX} Failed to register TTS engine {module_name}: {exc}"
                    )

    _register_from_path(base_path, "engines.external_engines")

    if dev_enabled:
        dev_path = os.path.join(base_path, "dev")
        if os.path.isdir(dev_path):
            _register_from_path(dev_path, "engines.external_engines.dev")
