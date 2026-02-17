from core.config_manager import config_registry


def test_exposed_variables_have_label_description_and_component():
    defs = config_registry.export_definitions()
    problems = []

    # Helper expected component for common prefixes
    prefix_map = {
        "MATRIX_": "matrix_chat",
        "TELEGRAM_": "telegram_bot",
        "DISCORD_": "discord_bot",
        "SELKIES_": "selkies",
        "SYNTH_": "persona",
        "RECON_": "agent",
    }

    for d in defs:
        key = d.get("key")
        label = d.get("label")
        desc = d.get("description")
        comp = d.get("component")

        # Only check exposed variables (most are), skip hidden/internal bootstrap
        # but still flag empties
        if not label or not str(label).strip():
            problems.append((key, "missing_label"))
        if not desc or not str(desc).strip():
            problems.append((key, "missing_description"))
        if not comp or not str(comp).strip():
            problems.append((key, "missing_component"))

        # Suggest component based on prefix
        for pfx, expected in prefix_map.items():
            if key.startswith(pfx) and comp != expected:
                problems.append((key, f"unexpected_component (expected {expected}, got {comp})"))

    if problems:
        summary_lines = [f"{k}: {reason}" for k, reason in problems]
        full = "\n".join(summary_lines)
        raise AssertionError("Exposed variables audit failed:\n" + full)
