"""Vessel action-whitelist helper (plugin-owned, self-contained).

During a Rift Vessel embodiment turn the full ~60-action catalog is folded into
the system prompt and pushes it past the downstream char-budget clamp (see
``core/external_endpoints/bridges/cortex_bridge.py``). The clamp never trims the
system message — only the user body — so the small will/reflection prompt in the
body gets erased and SyntH authors goals "blind" (low-level material gathering).

The fix is a **whitelist** applied only on vessel turns that keeps the prompt
lean. It has two parts:

* **Hardcoded, non-editable** — the Vessel's own verbs (``vessel_*``) and the
  currently-connected world's verbs (``*_<world>_*``, e.g. ``*_minecraft_*``).
  These are imperative to embodiment, so the user cannot remove them. The game
  patterns are derived structurally from the connected world token (never a
  hardcoded game name), so they swap automatically when the connected world
  changes.
* **Editable** — the ``VESSEL_ACTION_WHITELIST`` config variable holds only the
  optional *core-extra* actions (message/event/... ) the user may tune.

Matching is structural (``fnmatch`` on the action *name*), never keyword/regex
intent detection — safe in a multi-language deployment. This module lives inside
the plugin (not core) so the whole feature is self-contained: if the Rift Vessel
plugin is absent, the core simply falls back to its scope-based derive.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

__all__ = [
    "parse_patterns",
    "matches_whitelist",
    "hardcoded_vessel_patterns",
    "DEFAULT_RECON_WHITELIST",
    "vessel_recon_whitelist_patterns",
]

#: Default value baked into the ``VESSEL_ACTION_WHITELIST`` config variable.
#: Only the optional *core-extra* actions — the vessel/game verbs are hardcoded
#: separately and never appear here.
DEFAULT_WHITELIST = "send_message, event, schedule_message, blocklist, spawn_drone"

#: Default value baked into the ``VESSEL_RECON_WHITELIST`` config variable — the
#: preflight (recon) counterpart of ``VESSEL_ACTION_WHITELIST``. During an
#: in-world embodiment turn only recon plugins whose ``get_recon_key()`` matches
#: one of these patterns participate in the combined recon LLM call; the noisy
#: research-oriented plugins (web search, agent-intent, video, channel resolver)
#: are excluded because the weaker embodiment model tends to verbalise their
#: "do a web search" style plan as its in-world reply instead of acting. The
#: kept keys are language/tone hints, memory search, and any ``vessel_*`` recon
#: key. Matching is structural (:func:`fnmatch.fnmatchcase` on the recon key),
#: never keyword/regex intent detection.
DEFAULT_RECON_WHITELIST = "language_hint, tone_hint, memory_search, vessel_*"


def parse_patterns(raw: str | None) -> list[str]:
    """Parse a raw whitelist string into a clean list of fnmatch patterns.

    Accepts comma- and/or newline-separated patterns. Whitespace is stripped and
    empty tokens are dropped. Fully fail-safe: any non-string input yields an
    empty list.
    """
    if not raw or not isinstance(raw, str):
        return []
    tokens: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        token = line.strip()
        if token:
            tokens.append(token)
    return tokens


def matches_whitelist(action_name: str, patterns: list[str]) -> bool:
    """Return whether ``action_name`` matches any fnmatch pattern.

    Structural name matching via :func:`fnmatch.fnmatchcase` — never keyword or
    regex intent detection. Fail-safe: an empty/invalid pattern list or a
    non-string name returns ``False``.
    """
    if not action_name or not isinstance(action_name, str) or not patterns:
        return False
    for pattern in patterns:
        try:
            if fnmatchcase(action_name, pattern):
                return True
        except Exception:  # pragma: no cover - defensive
            continue
    return False


def hardcoded_vessel_patterns(world: str | None) -> list[str]:
    """Return the always-included, non-editable vessel/game patterns.

    * ``vessel_*`` — the Vessel's own world-agnostic verbs (already namespaced
      ``vessel_<world>_<verb>``, so this also covers the connected world's
      verbs).
    * ``*_<world>_*`` — the connected world's verbs, expressed structurally from
      the ``world`` token (never a hardcoded game name), so they swap with the
      connected world. Omitted when no world is resolvable.

    Fail-safe: a missing/blank/generic world token yields just ``["vessel_*"]``.
    """
    patterns = ["vessel_*"]
    token = str(world or "").strip()
    if token and token not in {"vessel", "disabled"}:
        patterns.append(f"*_{token}_*")
    return patterns


def vessel_recon_whitelist_patterns() -> list[str]:
    """Return the recon-key allow patterns for an in-world embodiment turn.

    Reads the user-editable ``VESSEL_RECON_WHITELIST`` config variable (component
    ``vessel_plugin``), falling back to :data:`DEFAULT_RECON_WHITELIST` when it
    is empty/unset. The result is passed to :func:`matches_whitelist` against
    each recon plugin's ``get_recon_key()`` so only vessel-safe contributions
    reach the combined recon LLM call. Fully fail-safe: any error yields the
    parsed default patterns.
    """
    raw: str | None = None
    try:
        from core.config_manager import config_registry as _cfg

        raw = _cfg.get_value(
            "VESSEL_RECON_WHITELIST",
            DEFAULT_RECON_WHITELIST,
            value_type=str,
            component="vessel_plugin",
            group="plugins",
            advanced=True,
        )
    except Exception:  # pragma: no cover - defensive
        raw = None
    patterns = parse_patterns(raw)
    if not patterns:
        patterns = parse_patterns(DEFAULT_RECON_WHITELIST)
    return patterns
