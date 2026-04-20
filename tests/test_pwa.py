def test_manifest_display_fullscreen():
    from pathlib import Path

    manifest = Path("res/synth_webui/static/manifest.webmanifest").read_text(
        encoding="utf-8"
    )
    assert '"display": "fullscreen"' in manifest
    # ensure we include maskable icons for full-bleed install on Android
    assert "maskable" in manifest or "purpose" in manifest


def test_service_worker_exists_and_registered():
    from pathlib import Path

    shell_tpl = Path("core/webui_templates/synth_webui_shell.html").read_text(
        encoding="utf-8"
    )
    base_tpl = Path("core/webui_templates/base.html").read_text(encoding="utf-8")
    init_js = Path("res/synth_webui/js/init.js").read_text(encoding="utf-8")

    assert "manifest.webmanifest" in shell_tpl
    assert "manifest.webmanifest" in base_tpl
    assert "serviceWorker" in init_js and "service-worker.js" in init_js
    assert Path("res/synth_webui/static/service-worker.js").exists()
    # Ensure generated icons exist
    assert Path("res/synth_webui/static/synth_icon_180.png").exists()
    assert Path("res/synth_webui/static/synth_icon_192.png").exists()
    assert Path("res/synth_webui/static/synth_icon_512.png").exists()
    # install button presence
    assert "synth-install-btn" in shell_tpl
    assert "synth-install-btn" in init_js
    assert "__synthDeferredPrompt" in init_js
