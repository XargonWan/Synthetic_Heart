import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_main_js_contains_tags_and_combobox_handlers():
    path = ROOT / "res" / "synth_webui" / "js" / "main.js"
    content = path.read_text(encoding="utf-8")

    assert "item.ui_type === 'tags'" in content or 'item.ui_type === "tags"' in content
    assert (
        "item.ui_type === 'tag-combobox'" in content
        or 'item.ui_type === "tag-combobox"' in content
    )
    assert (
        "item.ui_type === 'combobox'" in content
        or 'item.ui_type === "combobox"' in content
    )
