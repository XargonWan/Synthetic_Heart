"""One instance per plugin class, even when two modules export it.

`plugins/grillo_plugin.py` is a backward-compatibility shim that re-exports the
`GrilloPlugin` defined in `plugins/grillo/grillo_impl.py`. Discovery walks every
file under `plugins/`, so it reached that class twice and built a second instance
which then ran its own `start()` - observed live as four Grillo startups and four
LLM-failure recovery loops at boot, recovering one failed turn four times.
"""

from __future__ import annotations


class _CountedPlugin:
    builds = 0

    def __init__(self) -> None:
        type(self).builds += 1


def test_a_class_re_exported_by_two_modules_is_built_once() -> None:
    from core.core_initializer import _instantiate_plugin_once

    seen: dict[object, object] = {}

    first, created_first = _instantiate_plugin_once(
        _CountedPlugin, seen, "plugins.grillo.grillo_impl"
    )
    second, created_second = _instantiate_plugin_once(
        _CountedPlugin, seen, "plugins.grillo_plugin"
    )

    assert first is second
    assert created_first is True
    assert created_second is False
    assert _CountedPlugin.builds == 1


def test_distinct_classes_still_get_distinct_instances() -> None:
    from core.core_initializer import _instantiate_plugin_once

    class _Other(_CountedPlugin):
        pass

    seen: dict[object, object] = {}

    first, _ = _instantiate_plugin_once(_CountedPlugin, seen, "plugins.a_plugin")
    second, _ = _instantiate_plugin_once(_Other, seen, "plugins.b_plugin")

    assert first is not second
    assert isinstance(first, _CountedPlugin)
    assert isinstance(second, _Other)


def test_the_grillo_shim_still_re_exports_the_real_class() -> None:
    """The premise of the test above, asserted against the real modules."""
    from plugins.grillo.grillo_impl import GrilloPlugin
    from plugins.grillo_plugin import PLUGIN_CLASS

    assert PLUGIN_CLASS is GrilloPlugin
