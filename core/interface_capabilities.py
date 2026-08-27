"""Structural interface-capability introspection for the dispatch gate.

Before dispatching a message/audio action to an interface, the action parser
must know whether that interface can actually deliver it. Today that knowledge
is implicit — a missing ``send_message`` method makes dispatch fall through
silently. This module makes the capability *structural and checkable*.

Design rules (per AGENTS.md):

* **Pure and side-effect-free.** ``interface_capabilities()`` never touches the
  DB, message chain, or network; it only reflects on the interface object.
  Registering the config var is the only import-time side effect (same pattern
  as ``core/delivery_guard.py``).
* **Fail-open.** A broken or partially-introspectable interface must never be
  blocked from receiving a dispatch. ``has_capability()`` returns ``True`` on
  any introspection error.
* **Structural tokens only.** Capability names are derived from method presence,
  an explicit ``get_capabilities()`` hook, and ``get_supported_actions()`` keys
  — never from message text or keywords.
"""

from __future__ import annotations

from typing import Any

from core.config_manager import config_registry
from core.variables_engine import register_exposed_var

register_exposed_var(
    "INTERFACE_CAPABILITY_GATE_ENABLED",
    label="Interface Capability Gate",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "Before dispatching a message/audio action to an interface, verify the "
        "interface actually advertises the required capability (send_message / "
        "send_audio / send_voice). Disable to restore the previous silent "
        "fall-through behaviour."
    ),
    scope="core",
    component="core",
)

# Structural method → capability-token mapping. ``send_tts_audio`` is a
# specialisation of audio delivery, so it implies the ``send_audio`` capability.
_CAPABILITY_METHOD_TOKENS: tuple[tuple[str, str], ...] = (
    ("send_message", "send_message"),
    ("send_audio", "send_audio"),
    ("send_file", "send_file"),
    ("send_voice", "send_voice"),
    ("send_tts_audio", "send_audio"),
)


def _declared_capabilities(iface: Any) -> frozenset[str] | None:
    """Return the explicit capability set from ``iface.get_capabilities()``, or
    ``None`` when the hook is absent. An empty declaration is respected as an
    explicit empty set (the interface advertises no capabilities)."""
    hook = getattr(iface, "get_capabilities", None)
    if hook is None:
        return None
    declared = hook()
    if declared is None:
        return frozenset()
    if isinstance(declared, str):
        return frozenset({declared})
    return frozenset(str(token) for token in declared)


def interface_capabilities(iface: Any) -> frozenset[str]:
    """Derive the structural capability tokens advertised by ``iface``.

    Resolution order:

    1. an explicit ``iface.get_capabilities()`` hook, when defined;
    2. presence of delivery methods (``send_message``, ``send_audio``,
       ``send_file``, ``send_voice``, ``send_tts_audio``);
    3. action names from ``iface.get_supported_actions()`` keys.
    """
    if iface is None:
        return frozenset()

    declared = _declared_capabilities(iface)
    if declared is not None:
        return declared

    caps: set[str] = set()
    for method, token in _CAPABILITY_METHOD_TOKENS:
        attr = getattr(iface, method, None)
        if callable(attr):
            caps.add(token)

    get_actions = getattr(iface, "get_supported_actions", None)
    if callable(get_actions):
        supported = get_actions()
        if isinstance(supported, dict):
            caps.update(str(name) for name in supported)

    return frozenset(caps)


def has_capability(iface: Any, cap: str) -> bool:
    """Return ``True`` when ``iface`` advertises ``cap``.

    Fail-open: any exception raised while introspecting the interface resolves
    to ``True``, so a broken interface is never blocked from receiving a
    dispatch.
    """
    try:
        return cap in interface_capabilities(iface)
    except Exception:
        return True


def capability_gate_enabled() -> bool:
    """Return ``True`` when the interface capability gate is active (default).

    Fail-open: a config read error resolves to ``True`` (the gate stays on, and
    the gate itself never blocks delivery because ``has_capability`` fails
    open). An explicitly disabled value resolves to ``False`` — the gate becomes
    a no-op and dispatch behaves exactly as before this change.
    """
    try:
        value = config_registry.get_value("INTERFACE_CAPABILITY_GATE_ENABLED", 1)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)
    except Exception:
        return True
