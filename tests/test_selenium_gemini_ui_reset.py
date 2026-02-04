from llm_engines.selenium_gemini import SeleniumGeminiPlugin


class DummyButton:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class DummyDriver:
    def __init__(self, url, buttons):
        self.current_url = url
        self._buttons = buttons

    def find_elements(self, by, selector):
        # Return the dummy buttons when asked for chat-history selector
        return self._buttons

    @property
    def window_handles(self):
        return [1]


def test_gemini_ui_reset_opens_new_chat_and_returns_retry(monkeypatch):
    # Create instance
    s = SeleniumGeminiPlugin()

    # Monkeypatch methods used in workflow
    # _ensure_logged_in -> True
    monkeypatch.setattr(s, "_ensure_logged_in", lambda driver: True)
    # _locate_prompt_area -> dummy
    monkeypatch.setattr(s, "_locate_prompt_area", lambda driver: object())
    # _send_prompt_with_confirmation -> True (sent)
    monkeypatch.setattr(
        s,
        "_send_prompt_with_confirmation",
        lambda textarea, prompt_text, **kwargs: True,
    )
    # _handle_response_choice -> no-op
    monkeypatch.setattr(s, "_handle_response_choice", lambda driver: None)
    # wait_until_response_stabilizes -> returns a refusal-like string
    monkeypatch.setattr(
        s,
        "wait_until_response_stabilizes",
        lambda driver,
        **kwargs: "Non sono a mio agio con questa conversazione, meglio interromperla.",
    )

    # Prepare dummy driver indicating gemini URL and with a clickable new-chat button
    btn = DummyButton()
    driver = DummyDriver(url="https://gemini.google.com/app/xyz", buttons=[btn])
    s.driver = driver

    # Call the plugin-specific hook directly (engine behavior)
    res = s._engine_ui_reset_hook(
        previous_response="Non sono a mio agio con questa conversazione, meglio interromperla."
    )

    # The result should indicate a gemini_ui_reset and the button should have been clicked
    assert isinstance(res, str)
    assert "gemini_ui_reset" in res
    assert btn.clicked is True
