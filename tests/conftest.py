import pytest


@pytest.fixture(autouse=True)
def _reset_animation_handler_singleton():
    """Ensure tests don't leak AnimationHandler singleton state across modules."""
    import core.animation_handler as ah

    ah._animation_handler = None
    yield
    ah._animation_handler = None
