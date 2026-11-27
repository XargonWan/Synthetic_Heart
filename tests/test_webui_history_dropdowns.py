from pathlib import Path


def test_history_dropdowns_not_rebuilt_from_empty_lists() -> None:
    """Filtered history responses carry [] for the dropdown lists (lazy-loaded);
    rebuilding from that wipes the dropdown until refresh."""
    text = Path("res/synth_webui/js/history.js").read_text(encoding="utf-8")
    assert "data.interface_paths.length > 0" in text
    assert "data.beat_types.length > 0" in text
    assert "data.environments.length > 0" in text
