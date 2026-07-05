# core/external_endpoints/models.py
"""Data models for external AI service endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EndpointProtocol(str, Enum):
    """Wire protocol used to communicate with the endpoint."""

    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    HARMONY = "harmony"
    CUSTOM = "custom"


class SubsystemType(str, Enum):
    """SyntH subsystem that an endpoint can be mapped to."""

    CORTEX = "cortex"
    VOX = "vox"
    AURIS = "auris"
    LIVE = "live"
    VISION = "vision"  # placeholder – not yet implemented


@dataclass
class ExternalEndpoint:
    """Represents a user-defined external AI service endpoint."""

    id: int
    name: str
    display_label: str
    protocol: EndpointProtocol
    base_url: str
    api_key_enc: str | None
    enabled: bool
    capabilities: dict[str, bool]
    subsystem_map: dict[str, bool]
    available_models: list[str]
    default_model: str | None
    probe_status: str  # 'never' | 'pending' | 'success' | 'failed'
    last_probe_at: str | None
    extra_config: dict[str, Any]
    models_metadata: list[dict[str, Any]] = field(default_factory=list)

    def effective_subsystem_map(self) -> dict[str, bool]:
        """Merge auto-probed capabilities with manual user overrides.

        User overrides (subsystem_map) take precedence over probe results.
        """
        merged: dict[str, bool] = {t.value: False for t in SubsystemType}
        merged.update({k: bool(v) for k, v in self.capabilities.items()})
        merged.update({k: bool(v) for k, v in self.subsystem_map.items()})
        return merged

    def engine_name(self) -> str:
        """Return the name used when registering in SyntH's registries."""
        return self.name

    @staticmethod
    def from_row(row: dict[str, Any]) -> "ExternalEndpoint":
        """Build an ExternalEndpoint from a raw DB row (dict with column names)."""
        import json

        def _json(val: Any, default: Any) -> Any:
            if val is None:
                return default
            if isinstance(val, (dict, list)):
                return val
            try:
                return json.loads(val)
            except Exception:
                return default

        raw_protocol = row.get("protocol") or "openai"
        try:
            protocol = EndpointProtocol(raw_protocol)
        except ValueError:
            protocol = EndpointProtocol.CUSTOM

        return ExternalEndpoint(
            id=row.get("id", 0),
            name=row.get("name", ""),
            display_label=row.get("display_label") or "",
            protocol=protocol,
            base_url=row.get("base_url") or "",
            api_key_enc=row.get("api_key_enc"),
            enabled=bool(row.get("enabled", True)),
            capabilities=_json(row.get("capabilities"), {}),
            subsystem_map=_json(row.get("subsystem_map"), {}),
            available_models=_json(row.get("available_models"), []),
            models_metadata=_json(row.get("models_metadata"), []),
            default_model=row.get("default_model"),
            probe_status=row.get("probe_status") or "never",
            last_probe_at=row.get("last_probe_at"),
            extra_config=_json(row.get("extra_config"), {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict.  The encrypted API key is omitted."""
        last_probe = self.last_probe_at
        if hasattr(last_probe, "isoformat"):
            last_probe = last_probe.isoformat()
        return {
            "id": self.id,
            "name": self.name,
            "display_label": self.display_label,
            "protocol": self.protocol.value,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key_enc),
            "enabled": self.enabled,
            "capabilities": self.capabilities,
            "subsystem_map": self.subsystem_map,
            "effective_subsystem_map": self.effective_subsystem_map(),
            "available_models": self.available_models,
            "models_metadata": self.models_metadata,
            "default_model": self.default_model,
            "probe_status": self.probe_status,
            "last_probe_at": str(last_probe) if last_probe else None,
            "extra_config": self.extra_config,
            "engine_name": self.engine_name(),
        }
