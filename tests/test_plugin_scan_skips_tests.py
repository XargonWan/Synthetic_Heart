"""The recursive plugin scan must never import a plugin's own test tree.

Importing ``plugins/<plugin>/tests/conftest.py`` (upstream's retroxr plugin
ships one) runs ``from tests.conftest import ...``, which imports the
repository's ``tests`` package. That package installs stand-ins for
``telegram``, ``telegram.error`` and ``telegram.ext`` into ``sys.modules`` and
imports pytest, so a live process binds the stub ``ApplicationBuilder`` and
``start_bot()`` dies with ``AttributeError: 'ApplicationBuilder' object has no
attribute 'token'`` (live 2026-11-17, 2D checkout).
"""

from __future__ import annotations

from pathlib import Path

from core.core_initializer import is_plugin_candidate

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("plugins", "cortex", "interface")


def _looks_like_test_code(path: Path) -> bool:
    return (
        "tests" in path.parts
        or "test" in path.parts
        or path.name == "conftest.py"
        or path.name.startswith("test_")
    )


def test_no_test_file_is_ever_a_plugin_candidate() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for name in SCAN_ROOTS
        for root in [REPO_ROOT / name]
        if root.exists()
        for path in root.rglob("*.py")
        if is_plugin_candidate(path) and _looks_like_test_code(path)
    ]

    assert offenders == []


def test_the_retroxr_plugin_loads_while_its_own_tests_do_not() -> None:
    plugin = REPO_ROOT / "plugins" / "rift_vessel" / "retroxr" / "retroxr.py"
    conftest = (
        REPO_ROOT / "plugins" / "rift_vessel" / "retroxr" / "tests" / "conftest.py"
    )

    assert plugin.exists(), "the retroxr plugin itself must keep loading"
    assert conftest.exists(), "the case that caused the outage must stay in the tree"

    assert is_plugin_candidate(plugin) is True
    assert is_plugin_candidate(conftest) is False


def test_a_checkout_under_a_tests_directory_still_loads_its_plugins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tests" / "synth" / "plugins"
    plugin = root / "demo" / "demo.py"
    plugin_test = root / "demo" / "tests" / "conftest.py"

    assert is_plugin_candidate(plugin, root) is True
    assert is_plugin_candidate(plugin_test, root) is False
