def test_manifest_display_fullscreen():
    from pathlib import Path
    manifest = Path('res/synth_webui/static/manifest.webmanifest').read_text(encoding='utf-8')
    assert '"display": "fullscreen"' in manifest
    # ensure we include maskable icons for full-bleed install on Android
    assert 'maskable' in manifest or 'purpose' in manifest


def test_service_worker_exists_and_registered():
    from pathlib import Path
    tpl = Path('core/webui_templates/synth_webui_shell_clean.html').read_text(encoding='utf-8')
    # service worker registration moved into res/synth_webui/js/init.js — check it instead of template
    js = Path('res/synth_webui/js/init.js').read_text(encoding='utf-8')
    assert 'serviceWorker' in js and 'service-worker.js' in js
    assert Path('res/synth_webui/static/service-worker.js').exists()
    # Ensure generated icons exist
    assert Path('res/synth_webui/static/synth_icon_180.png').exists()
    assert Path('res/synth_webui/static/synth_icon_192.png').exists()
    assert Path('res/synth_webui/static/synth_icon_512.png').exists()
    # install button presence still in template
    assert 'synth-install-btn' in tpl
