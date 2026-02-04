import pytest

from core import persona_manager


def _assemble_for_skin(pm, skin_name):
    pj = pm._load_persona_json(skin_name)
    assert pj is not None, f"persona.json for {skin_name} not found"
    profile = pm._assemble_profile_from_json(pj)
    return pj, profile


def test_rekku_profile_contains_parts():
    pm = persona_manager.PersonaManager(config={})
    pj, profile = _assemble_for_skin(pm, "Rekku")
    # Base template must be present
    base = persona_manager.SYNTH_BASE_PROFILE_TEMPLATE.format(
        name=pj.get("name", "SyntH")
    )
    assert base in profile
    # description and appearance must be included
    assert pj.get("description", "") in profile
    assert pj.get("attributes", {}).get("appearance", "") in profile


@pytest.mark.parametrize("skin", ["Zero", "Rei"])
def test_other_skins_profile_contains_parts(skin):
    pm = persona_manager.PersonaManager(config={})
    pj, profile = _assemble_for_skin(pm, skin)
    base = persona_manager.SYNTH_BASE_PROFILE_TEMPLATE.format(
        name=pj.get("name", "SyntH")
    )
    assert base in profile
    assert pj.get("description", "") in profile
    assert pj.get("attributes", {}).get("appearance", "") in profile


def test_profile_mentions_agency():
    pm = persona_manager.PersonaManager(config={})
    pj, profile = _assemble_for_skin(pm, "Rekku")
    assert ("agency" in profile.lower()) or ("autonom" in profile.lower())
