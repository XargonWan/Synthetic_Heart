"""Declarative setup panels for extensions.

Some extensions need a guided step that is not a plain setting, such as logging a user
account in with a code that arrives by SMS. An extension must **not** ship JavaScript
into the WebUI: that code would run with the operator's token and see everything the
page sees. Instead the extension describes the panel as data and the Core draws it.

The extension pushes a *state* with ``panels.set``; the Core validates it against a
small closed vocabulary (text, error, form, button), keeps the latest one per panel,
and serves it to the WebUI. When the person presses a button or sends a form the Core
calls ``panel.action`` on the extension with the values typed; the extension answers
and pushes the next state. Values are never logged.

State shape::

    {"title": "Account login",
     "blocks": [
        {"type": "text",   "text": "Connected as Alice."},
        {"type": "error",  "text": "The code was wrong."},
        {"type": "form",   "action": "verify_code", "submit": "Verify",
         "fields": [{"name": "code", "label": "Login code", "kind": "code"}]},
        {"type": "button", "action": "logout", "label": "Log out"}]}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param

CAP_PANEL = "ui.panel"

MAX_BLOCKS = 12
MAX_FIELDS = 6
MAX_TEXT = 600
MAX_LABEL = 80
MAX_VALUE = 400
FIELD_KINDS = ("text", "password", "tel", "code")

_ID = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_BLOCK_TYPES = ("text", "error", "form", "button")


def _text(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str):
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{what} must be a string")
    return value[:limit]


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise RpcError(
            ErrorCode.INVALID_PARAMS, f"{what} must be a lowercase identifier"
        )
    return value


def validate_state(raw: Any) -> dict[str, Any]:
    """Return a cleaned copy of a panel state, or raise ``INVALID_PARAMS``.

    Unknown keys are dropped, never passed through: what the Core serves is built
    here from the closed vocabulary.
    """
    if not isinstance(raw, dict):
        raise RpcError(ErrorCode.INVALID_PARAMS, "state must be an object")
    blocks_in = raw.get("blocks", [])
    if not isinstance(blocks_in, list) or len(blocks_in) > MAX_BLOCKS:
        raise RpcError(
            ErrorCode.INVALID_PARAMS, f"blocks must be a list of at most {MAX_BLOCKS}"
        )
    blocks: list[dict[str, Any]] = []
    for block in blocks_in:
        if not isinstance(block, dict) or block.get("type") not in _BLOCK_TYPES:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"a block must be an object of type {', '.join(_BLOCK_TYPES)}",
            )
        kind = block["type"]
        if kind in ("text", "error"):
            blocks.append(
                {"type": kind, "text": _text(block.get("text", ""), MAX_TEXT, "text")}
            )
        elif kind == "button":
            blocks.append(
                {
                    "type": "button",
                    "action": _ident(block.get("action"), "button action"),
                    "label": _text(block.get("label", ""), MAX_LABEL, "label"),
                }
            )
        else:
            fields_in = block.get("fields", [])
            if not isinstance(fields_in, list) or len(fields_in) > MAX_FIELDS:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    f"a form has at most {MAX_FIELDS} fields",
                )
            fields: list[dict[str, str]] = []
            for item in fields_in:
                if (
                    not isinstance(item, dict)
                    or item.get("kind", "text") not in FIELD_KINDS
                ):
                    raise RpcError(
                        ErrorCode.INVALID_PARAMS,
                        f"a field is an object of kind {', '.join(FIELD_KINDS)}",
                    )
                fields.append(
                    {
                        "name": _ident(item.get("name"), "field name"),
                        "label": _text(item.get("label", ""), MAX_LABEL, "label"),
                        "kind": item.get("kind", "text"),
                        "placeholder": _text(
                            item.get("placeholder", ""), MAX_LABEL, "placeholder"
                        ),
                        # A password is never echoed back to the browser.
                        "value": ""
                        if item.get("kind") in ("password", "code")
                        else _text(item.get("value", ""), MAX_VALUE, "value"),
                    }
                )
            blocks.append(
                {
                    "type": "form",
                    "action": _ident(block.get("action"), "form action"),
                    "submit": _text(block.get("submit", "OK"), MAX_LABEL, "submit"),
                    "fields": fields,
                }
            )
    return {"title": _text(raw.get("title", ""), MAX_LABEL, "title"), "blocks": blocks}


@dataclass
class PanelStore:
    """The latest state of every panel, by ``(extension id, interface id)``."""

    states: dict[tuple[str, str], dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )

    def set(
        self, extension_id: str, interface_id: str, panel_id: str, state: dict[str, Any]
    ) -> None:
        self.states.setdefault((extension_id, interface_id), {})[panel_id] = state

    def for_interface(self, interface_id: str) -> list[dict[str, Any]]:
        """Every panel attached to ``interface_id``, as ``{extension, panel, state}``."""
        found: list[dict[str, Any]] = []
        for (extension_id, iface), panels in sorted(self.states.items()):
            if iface != interface_id:
                continue
            for panel_id, state in sorted(panels.items()):
                found.append(
                    {"extension": extension_id, "panel": panel_id, "state": state}
                )
        return found

    def drop_extension(self, extension_id: str) -> None:
        for key in [k for k in self.states if k[0] == extension_id]:
            del self.states[key]


def clean_values(raw: Any) -> dict[str, str]:
    """Form values from the browser: at most ``MAX_FIELDS`` short strings."""
    if not isinstance(raw, dict):
        return {}
    values: dict[str, str] = {}
    for key, value in list(raw.items())[:MAX_FIELDS]:
        if isinstance(key, str) and _ID.match(key) and isinstance(value, str):
            values[key] = value[:MAX_VALUE]
    return values


async def run_action(
    gateway: Gateway,
    extension_id: str,
    interface_id: str,
    panel_id: str,
    action: str,
    values: dict[str, str],
) -> dict[str, Any]:
    """Tell the extension the person did something in its panel."""
    session = gateway.sessions.get(extension_id)
    if session is None:
        return {"ok": False, "error": "the extension is not running"}
    try:
        result = await session.call(
            "panel.action",
            {
                "interface_id": interface_id,
                "panel": panel_id,
                "action": action,
                "values": values,
            },
            timeout=60.0,
        )
    except RpcError as exc:
        return {"ok": False, "error": exc.message[:200]}
    return {
        "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
        "error": str(result.get("error", ""))[:200] if isinstance(result, dict) else "",
    }


def register_panel_api(gateway: Gateway, store: PanelStore) -> None:
    """Register ``panels.set`` on the gateway."""

    async def set_panel(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        interface_id = args["interface_id"]
        declared = {i.id for i in ctx.manifest.provides.interfaces}
        if interface_id not in declared:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"interface {interface_id!r} is not declared in the manifest",
            )
        store.set(
            ctx.extension_id, interface_id, args["panel"], validate_state(args["state"])
        )
        return {"ok": True}

    gateway.register(
        ApiMethod(
            "panels.set",
            CAP_PANEL,
            (
                Param(
                    "interface_id",
                    max_length=64,
                    pattern=re.compile(r"[a-z][a-z0-9_]*"),
                ),
                Param("panel", max_length=41, pattern=_ID),
                Param("state", "object"),
            ),
            set_panel,
            "Show or update a setup panel on one of the extension's interfaces.",
        )
    )
    gateway.on_attach.append(
        lambda session: session.on_close(lambda: _forget(store, session.extension_id))
    )


async def _forget(store: PanelStore, extension_id: str) -> None:
    store.drop_extension(extension_id)
