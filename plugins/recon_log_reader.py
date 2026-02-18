from __future__ import annotations

import os
from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text


display_name = "Recon Log Reader"

# Expose UI config for the plugin
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_LOG_READER_RECON_ENABLED",
        label="Enable Recon Log Reader",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Recon Log Reader plugin (include log snippets in Recon).",
        scope="agent",
        component="agent",
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_LOG_READER_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Log Reader",
        description="Enable the Recon Log Reader plugin (include log snippets in Recon).",
        group="agent",
        component="agent",
    )

LOG_DIR = os.getenv("SYNTH_LOG_DIR", "logs")
ALLOWED_FILES = [
    "synth.log",
    "prompt_cycle.log",
    "webui.log",
    "selkies.log",
]
for i in range(1, 4):
    ALLOWED_FILES.append(f"synth.log.{i}")


def _tail_lines(path: str, lines: int = 80) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return "".join(data[-lines:])
    except Exception:
        return ""


class ReconLogReaderPlugin:
    display_name = display_name
    recon_priority = 4

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "log_request"

    def get_recon_instruction(self) -> str:
        return (
            "Determine if the user is asking for system logs. "
            'Return as an object: {"needs_logs": true|false}.'
        )

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
    ) -> list[dict]:
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_LOG_READER_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        if not isinstance(data, dict):
            return []

        needs_logs = bool(data.get("needs_logs", False))
        if not needs_logs:
            return []

        contributions: list[dict] = [
            {
                "type": "log_flag",
                "content": True,
                "source": "log_reader",
                "priority": int(self.recon_priority),
            }
        ]

        lines = int(
            config_registry.get_value("RECON_LOG_READER_LINES", 80, value_type=int)
            or 80
        )

        for filename in ALLOWED_FILES:
            path = os.path.join(LOG_DIR, filename)
            if not os.path.isfile(path):
                continue
            body = _tail_lines(path, lines=lines)
            if not body:
                continue
            if len(body) > 18000:
                body = "... (truncated)\n" + body[-18000:]
            contributions.append(
                {
                    "type": "snippet",
                    "content": f"[{filename}]\n{body}",
                    "source": "logs",
                    "priority": int(self.recon_priority),
                }
            )

        log_info(f"[recon_logs] Added {len(contributions) - 1} log snippet(s) to recon")
        return contributions

    async def get_recon_contributions(
        self,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
    ) -> list[dict]:
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_LOG_READER_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        engine = None
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            active_cortex = await get_active_cortex_engine()
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(f"[recon_logs] Failed to load active Cortex engine: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return []

        system_prompt = (
            "This is a Recon prompt, please execute what is requested below:\n"
            "- Determine if the user is asking for system logs.\n"
            'Return ONLY valid JSON: {"needs_logs": true|false}.'
        )

        try:
            llm_text = await engine.generate_response(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text.strip()},
                ]
            )
        except Exception as e:
            log_warning(f"[recon_logs] LLM generate_response failed: {e}")
            return []

        parsed = None
        try:
            parsed = extract_json_from_text(llm_text, return_metadata=False)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            return []

        needs_logs = bool(parsed.get("needs_logs", False))
        if not needs_logs:
            return []

        contributions: list[dict] = [
            {
                "type": "log_flag",
                "content": True,
                "source": "log_reader",
                "priority": int(self.recon_priority),
            }
        ]

        lines = int(
            config_registry.get_value("RECON_LOG_READER_LINES", 80, value_type=int)
            or 80
        )

        for filename in ALLOWED_FILES:
            path = os.path.join(LOG_DIR, filename)
            if not os.path.isfile(path):
                continue
            body = _tail_lines(path, lines=lines)
            if not body:
                continue
            if len(body) > 18000:
                body = "... (truncated)\n" + body[-18000:]
            contributions.append(
                {
                    "type": "snippet",
                    "content": f"[{filename}]\n{body}",
                    "source": "logs",
                    "priority": int(self.recon_priority),
                }
            )

        log_info(f"[recon_logs] Added {len(contributions) - 1} log snippet(s) to recon")
        return contributions


PLUGIN_CLASS = ReconLogReaderPlugin
