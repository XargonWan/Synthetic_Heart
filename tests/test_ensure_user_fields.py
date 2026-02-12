from types import SimpleNamespace
from core.user_utils import ensure_message_user_fields


def test_ensure_fields_on_simple_namespace():
    msg = SimpleNamespace()
    msg.from_user = SimpleNamespace(id=123, username=None, first_name=None)
    ensure_message_user_fields(msg)
    assert getattr(msg.from_user, "full_name", None) is not None
    assert getattr(msg.from_user, "first_name", None) is not None


def test_ensure_on_none_user():
    msg = SimpleNamespace()
    msg.from_user = None
    ensure_message_user_fields(msg)
    assert getattr(msg.from_user, "id", None) == 0
    assert getattr(msg.from_user, "full_name", None) is None or isinstance(
        msg.from_user.full_name, str
    )
