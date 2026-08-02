"""Simple tests to assert mobile-friendly chat behavior is present in templates."""

from pathlib import Path


def test_mobile_auto_restore_comment_present():
    legacy_tpl = Path("core/webui_templates/synth_webui_index.html").read_text(
        encoding="utf-8"
    )
    shell_tpl = Path("core/webui_templates/synth_webui_shell.html").read_text(
        encoding="utf-8"
    )
    base_tpl = Path("core/webui_templates/base.html").read_text(encoding="utf-8")
    home_tpl = Path("core/webui_templates/sections/home.html").read_text(
        encoding="utf-8"
    )
    main_js = Path("res/synth_webui/js/main.js").read_text(encoding="utf-8")
    chat_window_js = Path("res/synth_webui/js/chat-window.mjs").read_text(
        encoding="utf-8"
    )

    # Legacy index remains present, but runtime behavior moved into shell + modular JS.
    assert "runtime uses synth_webui_shell.html + modular JS" in legacy_tpl

    # Chat restore button and home-stage layout live in current shell/home templates.
    assert ".chat-toggle-btn" in shell_tpl
    assert ".home-stage" in shell_tpl
    assert "var(--topbar-height" in shell_tpl and "calc(100vh -" in shell_tpl
    assert 'id="chat"' in home_tpl

    # Mobile navigation overlay behavior lives in base.html now.
    assert "@media (max-width: 768px)" in base_tpl
    assert "nav.main-nav" in base_tpl
    assert "nav.main-nav.open" in base_tpl
    assert "position: fixed" in base_tpl
    assert "z-index: 20000" in base_tpl
    assert "border-bottom: none" in base_tpl

    # Chat window restore logic lives in the dedicated chat module, while main.js bootstraps it.
    assert "chat-window.mjs" in main_js
    assert "restoreChatState" in chat_window_js
    assert "localStorage" in chat_window_js
    assert "getBoundingClientRect" in main_js
