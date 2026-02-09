from pathlib import Path


def test_eyes_state_helpers_present():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    assert '_setEyesState' in tpl
    assert '_clearEyesState' in tpl
    assert 'synth_eyes_state_changed' in tpl


def test_eyes_state_cleanup_logs_present():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    assert 'Clearing eyesState and forcing eyes open after outro' in tpl
    assert 'Clearing eyesState and forcing eyes open after playOnce finish' in tpl
