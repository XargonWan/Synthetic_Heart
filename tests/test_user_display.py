from types import SimpleNamespace
from core.user_utils import get_user_display_name, get_user_usertag


def test_get_user_display_name_none():
    assert get_user_display_name(None) == "Unknown"


def test_get_user_display_name_full_name():
    user = SimpleNamespace(id=1, username="u1", first_name="F", full_name="Full Name")
    assert get_user_display_name(user) == "Full Name"


def test_get_user_display_name_first_name():
    user = SimpleNamespace(id=2, username="u2", first_name="F")
    assert get_user_display_name(user) == "F"


def test_get_user_display_name_username():
    user = SimpleNamespace(id=3, username="u3")
    assert get_user_display_name(user) == "u3"


def test_get_user_display_name_id_only():
    user = SimpleNamespace(id=4)
    assert get_user_display_name(user) == "4"


def test_get_user_usertag():
    user = SimpleNamespace(id=3, username="u3")
    assert get_user_usertag(user) == "@u3"
    user2 = SimpleNamespace(id=4)
    assert get_user_usertag(user2) == "(no tag)"
