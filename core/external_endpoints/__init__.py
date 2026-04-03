# core/external_endpoints/__init__.py
"""External AI service endpoints subsystem.

Provides helpers for registering, persisting, and bridging user-defined
external AI endpoints into SyntH's Cortex, Vox, Auris, and Live subsystems.

Quick usage::

    from core.external_endpoints import get_external_endpoint_registry

    registry = get_external_endpoint_registry()
    ep = await registry.add_endpoint(
        name="my-ollama",
        base_url="http://localhost:11435/v1",
        protocol="openai",
    )
"""

from core.external_endpoints.models import (
    EndpointProtocol,
    ExternalEndpoint,
    SubsystemType,
)
from core.external_endpoints.registry import (
    ExternalEndpointRegistry,
    get_external_endpoint_registry,
)

__all__ = [
    "EndpointProtocol",
    "ExternalEndpoint",
    "SubsystemType",
    "ExternalEndpointRegistry",
    "get_external_endpoint_registry",
]
