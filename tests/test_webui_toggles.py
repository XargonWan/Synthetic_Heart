import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_synth_webui_has_toggle_switch_selectors():
    path = ROOT / "core" / "webui_templates" / "synth_webui_index.html"
    content = path.read_text(encoding='utf-8')

    # The CSS should support both the immediate sibling .toggle-slider
    # and the case where the slider is wrapped inside a label that follows the input
    assert 'input[type="checkbox"]:checked + .toggle-switch .toggle-slider' in content
    assert 'input[type="checkbox"]:disabled + .toggle-switch .toggle-slider' in content


def test_base_template_has_toggle_switch_selectors():
    path = ROOT / "core" / "webui_templates" / "base.html"
    content = path.read_text(encoding='utf-8')

    assert 'input[type="checkbox"]:checked + .toggle-switch .toggle-slider' in content
    assert 'input[type="checkbox"]:disabled + .toggle-switch .toggle-slider' in content


def test_create_config_bool_sets_checked():
    # The dynamic creation code for boolean config rows should set input.checked
    path = ROOT / "core" / "webui_templates" / "synth_webui_index.html"
    content = path.read_text(encoding='utf-8')
    assert 'if (type === \'bool\')' in content
    assert 'input.checked = !!item.value;' in content


def test_register_exposed_var_preserves_component():
    from core.variables_engine import register_exposed_var, exposed_vars
    from core.config_manager import config_registry

    # Use a unique key unlikely to be present already
    key = 'TEST_EXPOSED_PLUGIN_COMPONENT'
    # Ensure not already present
    if key in config_registry._definitions:
        del config_registry._definitions[key]

    register_exposed_var(
        key,
        label='Test plugin var',
        default='x',
        component='my_test_plugin',
    )

    # The config registry should now contain the variable with the requested component
    assert key in config_registry._definitions
    assert config_registry._definitions[key].component == 'my_test_plugin'


def test_exposed_vars_component_assignments():
    from core.config_manager import config_registry

    expected = {
        'FAILED_MESSAGE_TEXT': 'core',
        'RESPONSE_TIMEOUT': 'core',
        'RESTRICT_ACTIONS': 'core',
        'TRAINER_IDS': 'core',
        'PROMPT_LOCATION': 'core',
        'CHAT_HISTORY': 'conversation',
        'DIARY_HISTORY_DAYS': 'diary',
        'REACT_WHEN_MENTIONED': 'reactions',
        'SYNTH_PROFILE': 'persona',
        'SYNTH_ALIASES': 'persona',
        'SYNTH_FULL_ALIASES': 'persona',
        'SYNTH_CURRENT_ANIMATION': 'animation',
    }

    missing = []
    mismatches = []
    for key, expected_component in expected.items():
        if key not in config_registry._definitions:
            missing.append(key)
            continue
        actual = config_registry._definitions[key].component
        if actual != expected_component:
            mismatches.append((key, actual, expected_component))

    assert not missing, f"Missing config definitions for keys: {missing}"
    assert not mismatches, f"Component mismatches found: {mismatches}"


def test_config_sections_collapsed_by_default_in_js():
    path = (pathlib.Path(__file__).resolve().parents[1] / "core" / "webui_templates" / "synth_webui_index.html")
    content = path.read_text(encoding='utf-8')
    # We expect grouped component cards to be collapsed by default
    assert 'card.classList.add(\'collapsed\')' in content


def test_synth_current_animation_is_readonly():
    from core.config_manager import config_registry
    defn = config_registry._definitions.get('SYNTH_CURRENT_ANIMATION')
    assert defn is not None, 'SYNTH_CURRENT_ANIMATION missing from registry'
    assert getattr(defn, 'readonly', False) is True


def test_card_content_wrapper_and_collapser_exists():
    import pathlib
    path = (pathlib.Path(__file__).resolve().parents[1] / "core" / "webui_templates" / "synth_webui_index.html")
    content = path.read_text(encoding='utf-8')
    assert '.card-content' in content
    assert 'function addCardCollapsers' in content


def test_chat_resizable_default_true():
    """The chat should be resizable by default (regression guard).

    This verifies that when no explicit exposed variable is set, the
    WebUI defaults to a resizable chat (historical behaviour).
    """
    from core.webui import WebUI

    w = WebUI(autostart=False)
    assert w._get_chat_resizable() is True


def test_no_decorative_diary_carets():
    path = (pathlib.Path(__file__).resolve().parents[1] / "core" / "webui_templates" / "synth_webui_index.html")
    content = path.read_text(encoding='utf-8')
    # Diary date groups should not contain the old decorative caret glyph
    assert '<span class="toggle-icon">' not in content


def test_config_control_buttons_are_textual():
    path = (pathlib.Path(__file__).resolve().parents[1] / "core" / "webui_templates" / "sections" / "config.html")
    content = path.read_text(encoding='utf-8')
    # The expand/collapse controls should use explicit labels (no decorative caret glyphs)
    assert 'id="config-expand-all"' in content and 'Expand' in content
    assert 'id="config-collapse-all"' in content and 'Collapse' in content
