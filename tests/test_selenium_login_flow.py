import asyncio

from cortex.selenium_engine.selenium_llm_base import SeleniumLLMBase


def test_ensure_selkies_running_returns_bool():
    inst = SeleniumLLMBase(notify_fn=None, config={})
    val = asyncio.get_event_loop().run_until_complete(inst.ensure_selkies_running())
    assert isinstance(val, bool)


def test_check_login_state_without_driver():
    inst = SeleniumLLMBase(notify_fn=None, config={})
    state = asyncio.get_event_loop().run_until_complete(inst.check_login_state())
    assert isinstance(state, dict)
    assert state.get("login_state") in ("unknown", "unlogged", "logged")
    assert isinstance(state.get("logged_in"), bool)
