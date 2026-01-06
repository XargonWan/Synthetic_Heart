import pytest

from core.config_manager import config_registry
import core.persona_manager as pm


def test_autonomy_default_is_suggest():
    """Default defined in config should be 'suggest'"""
    defn = config_registry._definitions.get("SYNTH_AUTONOMY_MODE")
    assert defn is not None, "SYNTH_AUTONOMY_MODE should be registered"
    assert defn.default == "suggest"


def test_autonomy_has_choices_constraint_and_exposed_combobox():
    defn = config_registry._definitions.get("SYNTH_AUTONOMY_MODE")
    assert defn is not None
    assert defn.constraints and "choices" in defn.constraints
    assert defn.constraints["choices"] == ["passive", "suggest", "whitelisted", "autonomous"]

    # Check exposed var registration for combobox UI
    from core.variables_engine import exposed_vars
    exposed_def = exposed_vars.get_definition("SYNTH_AUTONOMY_MODE")
    assert exposed_def is not None
    assert exposed_def.ui_type == "combobox"
    assert exposed_def.options == ["passive", "suggest", "whitelisted", "autonomous"]
    # Tooltip should explain difference between whitelisted and autonomous
    assert "automatically execute ONLY actions" in exposed_def.description
    assert "full autonomy" in exposed_def.description


@pytest.mark.asyncio
async def test_setting_invalid_autonomy_raises():
    # Attempt to set an invalid value should raise ValueError
    with pytest.raises(ValueError):
        await config_registry.set_value("SYNTH_AUTONOMY_MODE", "invalid")

    # Setting a valid choice should succeed
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "suggest")
    # Read back and ensure value changed
    assert str(pm.SYNTH_AUTONOMY_MODE) == "suggest"
