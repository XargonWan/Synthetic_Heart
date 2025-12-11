import asyncio
import pytest


def test_history_evaluator_importable():
    import plugins.grillo.history_evaluator as he
    assert hasattr(he, 'HistoryEvaluatorPlugin')


@pytest.mark.asyncio
async def test_evaluate_history_returns_string(monkeypatch):
    import plugins.grillo.history_evaluator as he

    # Create plugin instance
    plugin = he.HistoryEvaluatorPlugin()

    # Call evaluate_history with a likely-empty interface path; should return a string
    result = await plugin.evaluate_history("telegram_bot/0", entries=2)
    assert isinstance(result, str)
    assert len(result) > 0
