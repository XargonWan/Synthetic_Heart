#!/usr/bin/env python3
"""Ghost disable/enable keeps the module identity for self-registered aliases.

Regression test: a plugin self-registering as ``radio_host`` while its
module file yields ``radio_host_plugin`` left a ghost component record with
no ``module_name`` after a WebUI disable, so re-enable failed with
``unknown_plugin_module``.
"""

import asyncio


def test_disable_enable_self_registered_alias() -> None:
    from core.core_initializer import (
        ComponentStatus,
        PLUGIN_REGISTRY,
        core_initializer,
    )

    original_registry = dict(PLUGIN_REGISTRY)
    original_components = dict(core_initializer.components)
    original_loaded = list(core_initializer.loaded_plugins)
    real_instantiate = core_initializer._instantiate_and_register

    class FakeRadio:
        display_name = "Fake Radio"

        def get_supported_actions(self):
            return {}

    inst = FakeRadio()
    # __module__ of a test-local class is this test module; override the
    # attribute lookup by stamping the module path the loader would see.
    inst.__module__ = "plugins.radio_host.radio_host_plugin"
    PLUGIN_REGISTRY["radio_host"] = inst
    core_initializer.components.pop("radio_host", None)
    core_initializer.components.pop("radio_host_plugin", None)

    async def fake_instantiate(module_name, short_name):
        PLUGIN_REGISTRY[short_name] = inst
        return True

    try:
        core_initializer._instantiate_and_register = fake_instantiate  # type: ignore[method-assign]

        disabled = asyncio.run(core_initializer.disable_plugin("radio_host"))
        assert disabled.get("ok") is True

        ghost = core_initializer.components.get("radio_host")
        assert ghost is not None
        assert ghost.status == ComponentStatus.SKIPPED
        assert ghost.module_name == "plugins.radio_host.radio_host_plugin"

        enabled = asyncio.run(core_initializer.enable_plugin("radio_host"))
        assert enabled.get("ok") is True, enabled
    finally:
        core_initializer._instantiate_and_register = real_instantiate  # type: ignore[method-assign]
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(original_registry)
        core_initializer.components.clear()
        core_initializer.components.update(original_components)
        core_initializer.loaded_plugins[:] = original_loaded
        try:
            asyncio.run(core_initializer._build_actions_block())
        except Exception:
            pass
