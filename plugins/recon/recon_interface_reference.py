# plugins/recon/recon_interface_reference.py
"""Recon Interface Reference.

An LLM-gated Recon plugin that fires only when the user's message talks about a
chat *platform / interface* ("scrivimi su telegram", "post it on Discord").

The always-present ``[Interfaces]`` prompt line lists the active interface ids
and nothing more. This plugin adds the detail, but only when it is wanted:

1. The shared Recon LLM call is asked (via :meth:`get_recon_instruction`) to
   extract the interface / platform names the user refers to — semantically, in
   any language, with no keyword or regex matching.
2. :meth:`parse_recon_response` keeps the active interfaces whose id or display
   name matches a mention, and renders each with its ``interface_path`` format
   (``get_interface_path_hint``) and the paths already known for it
   (``get_recent_interface_paths``).

Only interfaces that are switched on are ever listed, so a runtime disable
removes an interface from this snippet with no restart.
"""

from __future__ import annotations

import json
import re
from typing import Any, List

from core.config_manager import config_registry
from core.interface_aliases import to_display_path
from core.interface_paths import describe_active_interfaces, get_recent_interface_paths
from core.logging_utils import log_debug, log_info, log_warning

display_name = "Recon Interface Reference"

#: Known paths shown per interface; the snippet stays a few lines long.
_MAX_KNOWN_PATHS = 3


def _register_var() -> None:
    try:
        from core.variables_engine import register_exposed_var

        register_exposed_var(
            "RECON_INTERFACE_REFERENCE_RECON_ENABLED",
            label="Enable Recon Interface Reference",
            default=True,
            value_type=bool,
            ui_type="bool",
            description=(
                "Enable the Recon Interface Reference plugin (list the active "
                "interfaces and their path format when the user talks about one)."
            ),
            scope="recon",
            component="recon_interface_reference",
            hidden=True,
        )
    except Exception:
        config_registry.get_var(
            "RECON_INTERFACE_REFERENCE_RECON_ENABLED",
            True,
            value_type=bool,
            label="Enable Recon Interface Reference",
            description="Enable the Recon Interface Reference plugin.",
            group="recon",
            component="recon_interface_reference",
            hidden=True,
        )


_register_var()


def _norm(value: str) -> str:
    """Lowercase and strip separators so 'Telegram Bot' == 'telegram_bot'."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def match_interfaces(
    mentions: list[str], interfaces: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Active interfaces named by ``mentions``.

    A mention matches an interface when, once separators are stripped, it is
    contained in the interface id or display name ("telegram" matches both
    ``telegram`` and ``telegram_bot``; "Telegram Bot" matches only the bot).
    """
    needles = [n for n in (_norm(m) for m in mentions) if n]
    if not needles:
        return []
    matched: list[dict[str, str]] = []
    for item in interfaces:
        hay = {_norm(item.get("id", "")), _norm(item.get("display_name", ""))} - {""}
        if any(n in h for n in needles for h in hay):
            matched.append(item)
    return matched


class ReconInterfaceReferencePlugin:
    display_name = display_name
    recon_priority = 6

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "interface_reference"

    def get_recon_instruction(self) -> str:
        return (
            "Determine whether the user's message talks about a chat platform "
            "or messaging interface as a place to write, send, reply or be "
            "reached (for example 'write me on Telegram', 'post it on Discord', "
            "'scrivimi sul bot'). Extract each platform or interface name "
            "exactly as the user expressed it, in any language. Return an "
            'object: {"interface_reference": ["name1", ...]}. If the message '
            "does not refer to any platform or interface, return an empty list."
        )

    # ------------------------------------------------------------------
    # Term extraction
    # ------------------------------------------------------------------

    def _terms_from_data(self, data: Any) -> list[str]:
        raw: Any = data
        if isinstance(data, dict):
            raw = data.get("interface_reference", data)
            if isinstance(raw, dict):
                raw = raw.get("interface_reference")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(t).strip() for t in raw if str(t).strip()]

    def _terms_from_raw_text(self, raw_text: str | None) -> list[str]:
        if not raw_text or not raw_text.strip():
            return []
        try:
            parsed = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            found = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not found:
                return []
            try:
                parsed = json.loads(found.group())
            except json.JSONDecodeError:
                return []
        return self._terms_from_data(parsed)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _known_paths(interface_id: str, recent: list[dict[str, Any]]) -> list[str]:
        prefix = f"{interface_id}/"
        lines: list[str] = []
        for row in recent:
            path = str(row.get("interface_path") or "")
            if not path.startswith(prefix):
                continue
            display = str(row.get("display") or "").strip()
            lines.append(f"{path} ({display})" if display and display != path else path)
            if len(lines) >= _MAX_KNOWN_PATHS:
                break
        return lines

    def _render(
        self, matched: list[dict[str, str]], recent: list[dict[str, Any]]
    ) -> str:
        blocks: list[str] = []
        for item in matched:
            iface_id = item["id"]
            head = f"- {iface_id} ({item.get('display_name') or iface_id})"
            if item.get("path_hint"):
                head += f" -> interface_path: {item['path_hint']}"
            lines = [head]
            known = self._known_paths(iface_id, recent)
            if known:
                lines.append("    known: " + "; ".join(known))
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    # ------------------------------------------------------------------
    # Recon entry point
    # ------------------------------------------------------------------

    async def parse_recon_response(
        self,
        data,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        enabled = bool(
            config_registry.get_value(
                "RECON_INTERFACE_REFERENCE_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        mentions = self._terms_from_data(data) or self._terms_from_raw_text(
            _raw_llm_text
        )
        if not mentions:
            log_debug("[recon_interface] No interface referenced.")
            return []

        matched = match_interfaces(mentions, describe_active_interfaces())
        if not matched:
            log_debug(f"[recon_interface] No active interface matches {mentions}")
            return []

        try:
            recent = await get_recent_interface_paths(limit=40)
        except Exception as exc:
            log_warning(f"[recon_interface] recent paths unavailable: {exc!r}")
            recent = []

        # Known paths are shown in their readable alias form (telegram/@jay).
        recent = [
            {**row, "interface_path": await to_display_path(row.get("interface_path"))}
            for row in recent
        ]
        content = self._render(matched, recent)
        if not content.strip():
            return []

        log_info(
            f"[recon_interface] Listed {len(matched)} interface(s) for "
            f"mention(s): {mentions}"
        )
        return [
            {
                "type": "snippet",
                "content": (
                    "The message talks about these interfaces (active now). To "
                    "write there use send_message with the interface_path:\n" + content
                ),
                "source": "recon_interface_reference",
                "priority": int(self.recon_priority),
            }
        ]


PLUGIN_CLASS = ReconInterfaceReferencePlugin
