from core.config_manager import config_registry


def test_exposed_variable_label_and_description_style():
    """Enforce basic style rules for exposed variable metadata.

    - `label` must be capitalized and must NOT end with a period.
    - `description` must be capitalized and MUST end with a single period.
    - `description` must not contain double spaces.
    """
    defs = config_registry.export_definitions()
    label_issues = []
    desc_issues = []

    for d in defs:
        key = d.get("key")
        label = (d.get("label") or "").strip()
        desc = (d.get("description") or "").strip()

        # Label: non-empty, capitalized, not ending with a period
        if not label:
            label_issues.append((key, "empty_label"))
        else:
            if not label[0].isupper():
                label_issues.append((key, f"label_not_capitalized: {label}"))
            if label.endswith("."):
                label_issues.append((key, f"label_ends_with_period: {label}"))

        # Description: non-empty, capitalized, ends with a period, no double spaces
        if not desc:
            desc_issues.append((key, "empty_description"))
        else:
            if not desc[0].isupper():
                desc_issues.append((key, f"description_not_capitalized: {desc}"))
            if not desc.endswith("."):
                desc_issues.append((key, f"description_missing_terminal_period: {desc}"))
            if "  " in desc:
                desc_issues.append((key, "description_contains_double_space"))

    if label_issues or desc_issues:
        lines = []
        if label_issues:
            lines.append("Label style issues:")
            lines += [f"  {k}: {p}" for k, p in label_issues]
        if desc_issues:
            lines.append("Description style issues:")
            lines += [f"  {k}: {p}" for k, p in desc_issues]
        raise AssertionError("\n".join(lines))
