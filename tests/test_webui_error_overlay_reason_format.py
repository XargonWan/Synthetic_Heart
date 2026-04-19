import pathlib


def test_webui_error_overlay_formats_error_reason():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "res" / "synth_webui" / "js" / "init.js").read_text(encoding="utf-8")

    # Ensure unhandledrejection handler formats Error objects without JSON.stringify(Error) => "{}"
    assert "function formatRejectionReason" in html
    assert "reason instanceof Error" in html
