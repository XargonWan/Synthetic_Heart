"""The diary compactor tells the model which beliefs the trainer retracted.

The compactor summarises history faithfully, so it must not drop a day's mention
of a retracted claim; it must record it as a mistake instead of a fact.
"""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.soul.retractions import KNOWN_FALSE_SUMMARY_INSTRUCTION
from plugins.grillo.grillo_compactor.grillo_compactor import GrilloCompactorPlugin


def _wire(monkeypatch: pytest.MonkeyPatch, block: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    class _Engine:
        async def generate_response(self, prompt: dict[str, Any]) -> str:
            seen.append(prompt)
            return json.dumps(
                {
                    "summary": "Short.",
                    "anchors": {},
                    "confidence": 0.95,
                    "feeling": "ok",
                }
            )

    class _Registry:
        def get_engine(self, name: str) -> _Engine:
            return _Engine()

    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", AsyncMock(return_value="e")
    )
    monkeypatch.setattr(
        "core.config.get_active_cortex_scope", AsyncMock(return_value="m")
    )
    monkeypatch.setattr("core.cortex_registry.get_cortex_registry", lambda: _Registry())
    monkeypatch.setattr(
        importlib.import_module("plugins.grillo.grillo_compactor.grillo_compactor"),
        "get_known_false_claims_block",
        AsyncMock(return_value=block),
    )
    return seen


async def _run(monkeypatch: pytest.MonkeyPatch, block: str) -> dict[str, Any]:
    seen = _wire(monkeypatch, block)
    plugin = GrilloCompactorPlugin()
    row = {
        "id": 7,
        "created_at": "2026-10-02",
        "content": "We talked about the trip to Ca' Senz'Autobus at length today. " * 5,
        "personal_thought": "",
    }
    await plugin._compact_one_day(row, dry_run=True)
    return seen[0]


@pytest.mark.asyncio
async def test_day_prompt_carries_the_known_false_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = await _run(monkeypatch, "These statements are FALSE: the trip")
    assert prompt["input"]["payload"]["known_false_claims"].startswith("These")
    assert KNOWN_FALSE_SUMMARY_INSTRUCTION in prompt["instructions"]


@pytest.mark.asyncio
async def test_day_prompt_is_unchanged_without_retractions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = await _run(monkeypatch, "")
    assert "known_false_claims" not in prompt["input"]["payload"]
    assert "known_false_claims" not in prompt["instructions"]
