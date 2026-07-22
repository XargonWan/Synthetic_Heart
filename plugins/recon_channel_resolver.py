# plugins/recon_channel_resolver.py
"""Recon Channel Resolver.

An LLM-gated Recon plugin that resolves channels *cited by name* in the user's
message into concrete ``interface_path`` values, grouped hierarchically.

Flow
----
1. The shared Recon LLM call is asked (via :meth:`get_recon_instruction`) to
   extract the human-readable NAMES of any chat/channel/server the user refers
   to — semantically, in any language, with no keyword or regex matching.
2. :meth:`parse_recon_response` feeds those names to
   :func:`core.interface_paths.find_channel_groups`, which matches them against
   the stored ``segment_labels`` of known interface paths.
3. Matches are rendered as a single hierarchical ``snippet`` contribution so
   Synth can pick the right ``interface_path`` when it wants to send a message
   to a channel the user only named (never quoted the raw id of).

Groups whose only rows are thread rows (no bare thread-less parent) are rendered
with a ``/*`` placeholder to signal that a thread segment is mandatory.
"""

from __future__ import annotations

import json
from typing import Any, List

from core.config_manager import config_registry
from core.interface_paths import find_channel_groups
from core.logging_utils import log_debug, log_info, log_warning

display_name = "Recon Channel Resolver"


def _register_var(
    name: str,
    *,
    label: str,
    default: Any,
    value_type: type,
    ui_type: str,
    description: str,
) -> None:
    try:
        from core.variables_engine import register_exposed_var

        register_exposed_var(
            name,
            label=label,
            default=default,
            value_type=value_type,
            ui_type=ui_type,
            description=description,
            scope="recon",
            component="recon",
        )
    except Exception:
        config_registry.get_var(
            name,
            default,
            value_type=value_type,
            label=label,
            description=description,
            group="recon",
            component="recon",
        )


_register_var(
    "RECON_CHANNEL_RESOLVER_RECON_ENABLED",
    label="Enable Recon Channel Resolver",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Enable the Recon Channel Resolver plugin (resolve channels the "
    "user refers to by name into concrete interface paths).",
)


class ReconChannelResolverPlugin:
    display_name = display_name
    recon_priority = 5

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "channel_reference"

    def get_recon_instruction(self) -> str:
        return (
            "Determine whether the user's message refers to any chat, channel, "
            "group, or server BY NAME (for example 'the Informatica channel', "
            "'il Dojo del Porcospino', or 'the tech server'). Extract each "
            "referenced name exactly as the user expressed it, in any language. "
            'Return an object: {"channel_reference": ["name1", "name2", ...]}. '
            "If the message does not refer to any channel by name, return an "
            "empty list."
        )

    # ------------------------------------------------------------------
    # Term extraction helpers
    # ------------------------------------------------------------------

    def _terms_from_data(self, data: Any) -> list[str]:
        """Pull the referenced channel names out of the parsed recon payload."""
        payload = data
        raw: Any = None
        if isinstance(payload, dict):
            inner = payload.get("channel_reference", payload)
            if isinstance(inner, list):
                raw = inner
            elif isinstance(inner, dict):
                raw = inner.get("channel_reference")
            elif isinstance(inner, str):
                raw = [inner]
        elif isinstance(payload, list):
            raw = payload
        elif isinstance(payload, str):
            raw = [payload]
        if not isinstance(raw, list):
            return []
        return [str(t).strip() for t in raw if str(t).strip()]

    def _terms_from_raw_text(self, raw_text: str | None) -> list[str]:
        """Fallback self-parse of the raw LLM text when central parsing missed."""
        if not raw_text or not raw_text.strip():
            return []
        try:
            parsed = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            import re

            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not match:
                return []
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        return self._terms_from_data(parsed)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_groups(self, groups: list[dict[str, Any]]) -> str:
        """Render resolved groups as a hierarchical, human-readable snippet."""
        blocks: list[str] = []
        for group in groups:
            label = group.get("group_label") or group.get("root_prefix")
            root_prefix = group.get("root_prefix")
            thread_required = bool(group.get("thread_required"))
            children = group.get("children") or []

            header_path = f"{root_prefix}/*" if thread_required else root_prefix
            lines = [f"{label} -> {header_path}"]
            if thread_required:
                lines.append(
                    "  (a thread segment is required; replace * with a thread id)"
                )
            for child in children:
                child_path = child.get("interface_path")
                child_display = child.get("display") or child_path
                lines.append(f"  - {child_display} -> {child_path}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

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
                "RECON_CHANNEL_RESOLVER_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        terms = self._terms_from_data(data)
        if not terms:
            terms = self._terms_from_raw_text(_raw_llm_text)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        terms = [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))]
        if not terms:
            log_debug("[recon_channel] No channel names referenced.")
            return []

        try:
            groups = await find_channel_groups(terms)
        except Exception as exc:
            log_warning(f"[recon_channel] find_channel_groups failed: {exc!r}")
            return []

        if not groups:
            log_debug(f"[recon_channel] No channels matched terms: {terms}")
            return []

        content = self._render_groups(groups)
        if not content.strip():
            return []

        log_info(
            f"[recon_channel] Resolved {len(groups)} channel group(s) "
            f"from {len(terms)} referenced name(s)"
        )
        return [
            {
                "type": "snippet",
                "content": (
                    "Channels referenced by name in the message resolve to these "
                    "interface paths:\n" + content
                ),
                "source": "recon_channel_resolver",
                "priority": int(self.recon_priority),
            }
        ]


PLUGIN_CLASS = ReconChannelResolverPlugin
