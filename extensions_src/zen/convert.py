"""What the Zen extension tells the Core about its endpoint (pure data)."""

from __future__ import annotations

ENDPOINT_NAME = "zen-llm-engine"
LABEL = "Zen LLM Engine"
DEFAULT_BASE_URL = "http://synth-zen-llm-engine:8000"
#: Container hostnames the endpoint may have been saved with before the rename
#: ("selenium-llm-engine" -> "zen-llm-engine"); they no longer resolve.
STALE_BASE_URLS = frozenset({"http://synth-selenium-llm-engine:8000"})

#: Settings Zen needs, applied only where the endpoint lacks them.
#:
#: ``downstream_char_budget``: Zen splits any prompt over about 32000 characters into
#: several parts, a path that garbles requests. The clamp counts message *content*, and
#: the serialized payload is thousands of characters larger, so the budget leaves that
#: headroom (measured against the engine on a heavy turn).
EXTRA_CONFIG = {"timeout": 300, "downstream_char_budget": 24000}
SUBSYSTEMS = {
    "cortex": True,
    "vox": False,
    "auris": False,
    "live": False,
    "vision": True,
}


def is_stale(base_url: str) -> bool:
    return base_url.rstrip("/") in STALE_BASE_URLS
