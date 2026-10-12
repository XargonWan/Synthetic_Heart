from pathlib import Path


def test_chat_replay_propagates_original_timestamp() -> None:
    """Replayed bubbles must carry the stored send time, not render as now."""
    text = Path("core/webui.py").read_text(encoding="utf-8")
    assert 'replay_payload["ts"] = item_ts' in text, (
        "replay payload should forward the stored timestamp as ts"
    )
