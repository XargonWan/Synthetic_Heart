"""Tests for the catalog-fallback action grammar on force_action_grammar endpoints.

The corrector sends its JSON-correction retries as a raw string prompt (no typed
PromptRequest). Without the fallback those retries lose the GBNF grammar, so a
small local model keeps emitting malformed/incomplete action JSON and the
corrector loop never converges. The fallback rebuilds the grammar from the full
registered action catalog — but only when the endpoint opted in, so other
engines are never affected.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.core_initializer import core_initializer
from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint


def _make_engine(extra_config: dict) -> ExternalCortexEngine:
    endpoint = ExternalEndpoint(
        id=1,
        name="local_ep",
        display_label="Local EP",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://127.0.0.1:8081",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"cortex": True},
        available_models=["m"],
        default_model="m",
        probe_status="success",
        last_probe_at=None,
        extra_config=extra_config,
    )
    return ExternalCortexEngine(endpoint, MagicMock())


def _seed_catalog(monkeypatch):
    monkeypatch.setattr(
        core_initializer,
        "actions_block",
        {"available_actions": {"message_telegram_bot": {}, "get_recent_chats": {}}},
    )


def test_string_prompt_gets_fallback_grammar_when_opted_in(monkeypatch):
    _seed_catalog(monkeypatch)
    engine = _make_engine({"force_action_grammar": True, "disable_tools": True})

    # A raw string prompt is exactly what the corrector passes on retries.
    kwargs = engine._tool_api_kwargs('{"actions": []}')

    grammar = kwargs.get("extra_body", {}).get("grammar")
    assert isinstance(grammar, str) and grammar
    # The action type enum is built from the registered catalog.
    assert "get_recent_chats" in grammar
    assert "message_telegram_bot" in grammar


def test_string_prompt_has_no_grammar_when_not_opted_in(monkeypatch):
    _seed_catalog(monkeypatch)
    # No force_action_grammar (e.g. a cloud OpenAI/Anthropic endpoint): the
    # corrector must NOT have a grammar injected.
    engine = _make_engine({})

    assert engine._tool_api_kwargs('{"actions": []}') == {}


def test_manual_grammar_suppresses_catalog_fallback(monkeypatch):
    _seed_catalog(monkeypatch)
    # A manual grammar in extra_config is applied via _extra_api_kwargs and wins,
    # so the catalog fallback must back off to avoid clobbering it.
    engine = _make_engine({"force_action_grammar": True, "grammar": "root ::= ..."})

    assert engine._tool_api_kwargs('{"actions": []}') == {}
