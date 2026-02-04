import pathlib


def test_webui_error_overlay_formats_error_reason():
    root = pathlib.Path(__file__).resolve().parents[1]
    js = (root / 'res' / 'synth_webui' / 'js' / 'init.js').read_text(encoding='utf-8')

    # Ensure the init helper includes an error formatting function that handles Error objects
    assert 'function formatRejectionReason' in js
    assert 'reason instanceof Error' in js
