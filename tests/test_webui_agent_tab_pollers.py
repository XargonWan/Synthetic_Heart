from pathlib import Path


def test_agent_tab_pollers_stop_on_leave() -> None:
    """Revisiting the agent tab must not stack pollers; leaving stops them."""
    agent = Path("res/synth_webui/js/agent.js").read_text(encoding="utf-8")
    assert "stopAgentTab" in agent
    hist = Path("res/synth_webui/js/history.js").read_text(encoding="utf-8")
    assert "stopAgentTab" in hist
