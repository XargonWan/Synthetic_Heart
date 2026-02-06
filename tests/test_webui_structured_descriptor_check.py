from pathlib import Path


def test_structured_descriptor_check_present():
    content = Path('res/synth_webui/js/vrm-viewer.mjs').read_text(encoding='utf-8')
    assert "typeof descriptor.intro.start_frame === 'number'" in content
    assert "typeof descriptor.outro.end_frame === 'number'" in content
