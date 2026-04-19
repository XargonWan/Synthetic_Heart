def test_chat_module_file_and_api():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "chat-window.mjs"
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert (
        "export async function createChatWindow" in txt
        or "export function createChatWindow" in txt
    ), "createChatWindow API not found in chat-window.mjs"


def test_main_imports_chat_module():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "import('./chat-window.mjs" in txt and "createChatWindow" in txt, (
        "main.js does not dynamically import chat-window.mjs"
    )
