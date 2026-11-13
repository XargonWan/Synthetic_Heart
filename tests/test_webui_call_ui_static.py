"""Static wiring checks for the call UI (no browser): header button, hidden mic,
and the pop-out "webcam" (close-up, landscape) that follows the call state."""

from pathlib import Path

JS = Path("res/synth_webui/js")


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def test_call_button_sits_in_chat_header_after_open_archives() -> None:
    chat = _read("chat-window.mjs")
    header = chat.index('<div class="synth-chat-archive">')
    restore = chat.index('id="chat-restore"', header)
    call = chat.index('id="call-btn"', header)
    footer = chat.index('class="synth-chat-footer')
    assert restore < call < footer
    # The footer no longer carries a call button.
    assert chat.count('id="call-btn"') == 1


def test_mic_button_hidden_during_a_call() -> None:
    chat = _read("chat-window.mjs")
    assert "const inCall = !!(callClient && callClient.active);" in chat
    assert chat.count("if (inCall) sendBtn.style.display = 'none';") == 2
    # Leaving/entering a call re-renders the button.
    assert "try { updateButtonMode(); } catch" in chat


def test_call_state_event_drives_the_popout() -> None:
    chat = _read("chat-window.mjs")
    pip = _read("pip-window.mjs")
    assert "synth:call-state" in chat and "synth:call-state" in pip
    # Landscape webcam in a call, portrait full body otherwise.
    assert "WEBCAM_SIZE = { width: 640, height: 360 }" in pip
    assert "FULL_BODY_SIZE = { width: 360, height: 640 }" in pip
    assert "callActive ? 'closeup' : 'full'" in pip
    # Closing the pop-out hands the camera back to the main window.
    assert "window.__vrmFramingMode = null" in pip


def test_viewer_has_closeup_and_full_framing_modes() -> None:
    viewer = _read("vrm-viewer.mjs")
    assert "fitCloseupFraming" in viewer
    assert "mode === 'closeup'" in viewer
    assert "aspect >= 1 && mode !== 'full'" in viewer


def test_pip_button_is_a_webcam_icon() -> None:
    home = Path("core/webui_templates/sections/home.html").read_text(encoding="utf-8")
    button = home[home.index('id="pip-toggle"') :]
    button = button[: button.index("</button>")]
    assert "<svg" in button and "\U0001f5bc" not in button


def test_chat_window_is_never_left_tiny_on_load() -> None:
    chat = _read("chat-window.mjs")
    assert "function _enforceChatMinHeight" in chat
    assert "setTimeout(() => _enforceChatMinHeight(winbox)" in chat


def test_signal_is_played_as_sfx_and_ignored_by_the_mic() -> None:
    chat = _read("chat-window.mjs")
    assert "data.type === 'sfx-play'" in chat
    assert "__synthIgnoreMicUntil" in chat
    # A blip shorter than the minimum is not sent, and an empty clip in a call is silent.
    assert "MIC_MIN_SPEECH_MS" in chat and "tooShort" in chat
    assert "call: empty transcription ignored" in chat


def test_video_call_makes_the_synth_look_into_the_camera() -> None:
    pip = _read("pip-window.mjs")
    viewer = _read("vrm-viewer.mjs")
    assert "const video = !!(callActive && pipWindow);" in pip
    assert "__synthSetCameraGazeLock" in pip and "__synthSetCameraGazeLock" in viewer
    # Taps must not pull the locked gaze away.
    assert "if (__synthFollowMouse.lockCamera) return;" in viewer


def test_client_sends_the_call_id_with_the_clip_and_has_no_vad_ack_path() -> None:
    chat = _read("chat-window.mjs")
    client = _read("call-client.mjs")
    assert "fd.append('call_id', callClient.callId)" in chat
    # The signal follows the transcription now, not the VAD.
    assert "cfg.call_id" not in chat and "notifyUtteranceEnd" not in chat + client
    assert "utterance_end" not in client
