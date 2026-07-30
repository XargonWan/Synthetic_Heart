"""Unit tests for the Rift Vessel *recon* whitelist.

Covers the pure recon-key allow patterns used to filter which recon plugins may
contribute to the combined recon LLM call during an in-world embodiment turn.
The whitelist is structural (``fnmatch`` on the plugin ``get_recon_key()``,
never message text) and keyword-free. No DB, bridge, or LLM is involved.
"""

from __future__ import annotations

from plugins.rift_vessel.vessel_whitelist import (
    DEFAULT_RECON_WHITELIST,
    matches_whitelist,
    parse_patterns,
    vessel_recon_whitelist_patterns,
)


class TestDefaultReconWhitelist:
    def test_default_contains_expected_recon_keys(self) -> None:
        assert "language_hint" in DEFAULT_RECON_WHITELIST
        assert "tone_hint" in DEFAULT_RECON_WHITELIST
        assert "memory_search" in DEFAULT_RECON_WHITELIST
        assert "vessel_*" in DEFAULT_RECON_WHITELIST

    def test_default_parses_to_four_patterns(self) -> None:
        patterns = parse_patterns(DEFAULT_RECON_WHITELIST)
        assert patterns == [
            "language_hint",
            "tone_hint",
            "memory_search",
            "vessel_*",
        ]


class TestVesselReconWhitelistPatterns:
    def test_returns_non_empty_list(self) -> None:
        patterns = vessel_recon_whitelist_patterns()
        assert isinstance(patterns, list)
        assert patterns  # never empty — falls back to the parsed default

    def test_falls_back_to_default_when_config_unavailable(self) -> None:
        # Even with no config backend wired in the test process, the helper must
        # degrade to the parsed DEFAULT_RECON_WHITELIST rather than raise.
        patterns = vessel_recon_whitelist_patterns()
        for expected in ("language_hint", "tone_hint", "memory_search", "vessel_*"):
            assert expected in patterns


class TestReconKeyMatching:
    """The recon-key allow patterns applied via ``matches_whitelist``."""

    def setup_method(self) -> None:
        self.patterns = parse_patterns(DEFAULT_RECON_WHITELIST)

    def test_vessel_safe_keys_allowed(self) -> None:
        for key in ("language_hint", "tone_hint", "memory_search"):
            assert matches_whitelist(key, self.patterns) is True

    def test_vessel_namespaced_keys_allowed(self) -> None:
        for key in (
            "vessel_needs_external_reach",
            "vessel_minecraft_lookup",
            "vessel_world_state",
        ):
            assert matches_whitelist(key, self.patterns) is True

    def test_out_of_world_keys_excluded(self) -> None:
        for key in (
            "web_search_needed",
            "agent_intent",
            "video_intent",
            "channel_lookup",
            "recon_generic",
        ):
            assert matches_whitelist(key, self.patterns) is False

    def test_empty_recon_key_excluded(self) -> None:
        assert matches_whitelist("", self.patterns) is False

    def test_matching_is_case_sensitive_structural(self) -> None:
        # fnmatchcase is case-sensitive: an upper-cased key must not sneak in.
        assert matches_whitelist("LANGUAGE_HINT", self.patterns) is False
