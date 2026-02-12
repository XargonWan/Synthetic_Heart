from core.agent_core import AgentLoopManager


def test_agent_does_not_expose_injection_helper():
    """Agents should not perform direct local-time injection; the prompt builder handles time context."""
    alm = AgentLoopManager()
    assert not hasattr(alm, "_inject_local_time_into_prompt"), "AgentLoopManager should not expose _inject_local_time_into_prompt"