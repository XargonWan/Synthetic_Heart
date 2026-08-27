from core.config_manager import config_registry


def test_exposed_variables_have_label_description_and_component():
    defs = config_registry.export_definitions()
    problems = []

    helper_prefixes = ("TEST_", "MOCK_")
    key_component_overrides = {
        "SYNTH_CURRENT_ANIMATION": "animation",
        "SYNTH_PEERS": "peer_policy",
        "SYNTH_PEER_POLICY": "peer_policy",
        "SYNTH_PEER_ENABLED": "peer_policy",
        "SYNTH_PEER_TURN_FLOOR_SECONDS": "peer_policy",
        "SYNTH_PEER_RELAY_TIMEOUT_SECONDS": "peer_policy",
        "SYNTH_PEER_MENTION_COOLDOWN_SECONDS": "peer_policy",
        "RECON_VIDEO_INCLUDE_VISION": "agent",
        "RECON_VIDEO_MAX_SECONDS": "agent",
        "RECON_VIDEO_SNIPPET_MAX_CHARS": "agent",
    }

    # Helper expected component for common prefixes
    prefix_map = {
        "MATRIX_": "matrix_chat",
        "TELEGRAM_": "telegram_bot",
        "DISCORD_": "discord_bot",
        "SYNTH_": "persona",
        "RECON_": "recon",
    }

    for d in defs:
        key = d.get("key")
        if not key or not isinstance(key, str):
            continue
        label = d.get("label")
        desc = d.get("description")
        comp = d.get("component")

        if key.startswith(helper_prefixes):
            continue

        # Only check exposed variables (most are), skip hidden/internal bootstrap
        # but still flag empties
        if not label or not str(label).strip():
            problems.append((key, "missing_label"))
        if not desc or not str(desc).strip():
            problems.append((key, "missing_description"))
        if not comp or not str(comp).strip():
            problems.append((key, "missing_component"))

        # Suggest component based on prefix
        expected_component = key_component_overrides.get(key)
        if (
            expected_component is None
            and key.startswith("RECON_")
            and key.endswith("_RECON_ENABLED")
        ):
            expected_component = "agent"
        if expected_component is None:
            for pfx, expected in prefix_map.items():
                if key.startswith(pfx):
                    expected_component = expected
                    break

        if expected_component is not None and comp != expected_component:
            problems.append(
                (
                    key,
                    f"unexpected_component (expected {expected_component}, got {comp})",
                )
            )

    if problems:
        summary_lines = [f"{k}: {reason}" for k, reason in problems]
        full = "\n".join(summary_lines)
        raise AssertionError("Exposed variables audit failed:\n" + full)
