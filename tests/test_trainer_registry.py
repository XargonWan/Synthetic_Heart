from core.interfaces_registry import InterfaceRegistry


def test_set_and_get_single_int():
    reg = InterfaceRegistry()
    reg.set_trainer_id("foo", 123)
    assert reg.get_trainer_id("foo") == 123
    assert reg.is_trainer("foo", 123)
    assert not reg.is_trainer("foo", 456)


def test_set_and_get_single_string():
    reg = InterfaceRegistry()
    reg.set_trainer_id("bar", "alice")
    assert reg.get_trainer_id("bar") == "alice"
    assert reg.is_trainer("bar", "alice")
    assert not reg.is_trainer("bar", "bob")


def test_set_list_mixed():
    reg = InterfaceRegistry()
    reg.set_trainer_id("baz", [123, "alice#0001"])
    stored = reg.get_trainer_id("baz")
    assert isinstance(stored, list)
    assert "123" in [str(x) for x in stored]
    assert "alice#0001" in stored

    assert reg.is_trainer("baz", 123)
    assert reg.is_trainer("baz", "alice#0001")
    assert not reg.is_trainer("baz", "not_in_list")


def test_is_trainer_none():
    reg = InterfaceRegistry()
    assert not reg.is_trainer("none", 1)
