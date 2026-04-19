from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_history_engine_ignores_cortex_switch_notifications(
    monkeypatch,
) -> None:
    from core.history_engine import HistoryEngine

    current_path = "synth_webui/current"

    context_memory = {
        current_path: deque(
            [
                {
                    "sender_name": "self",
                    "text": "✅ Cortex engine dynamically updated to `gemini`.",
                    "timestamp": "2026-04-19T00:08:00+00:00",
                    "interface_path": current_path,
                },
                {
                    "sender_name": "Scar",
                    "text": "hello there",
                    "timestamp": "2026-04-19T00:08:01+00:00",
                    "interface_path": current_path,
                },
            ]
        )
    }

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(
            return_value=deque(
                [
                    {
                        "sender_name": "self",
                        "text": "✅ Cortex engine dynamically updated to `openrouter`.",
                        "timestamp": "2026-04-19T00:04:00+00:00",
                        "interface_path": "synth_webui/other",
                    },
                    {
                        "sender_name": "Scar",
                        "text": "cross chat line",
                        "timestamp": "2026-04-19T00:05:00+00:00",
                        "interface_path": "synth_webui/other",
                    },
                ]
            )
        ),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory=context_memory,
        interface_name="synth_webui",
        text="current input",
    )

    joined_current = "\n".join(context["history_current_chat"])
    joined_recent = "\n".join(context["history_recent"])

    assert "hello there" in joined_current
    assert "cross chat line" in joined_recent
    assert "Cortex engine dynamically updated" not in joined_current
    assert "Cortex engine dynamically updated" not in joined_recent
