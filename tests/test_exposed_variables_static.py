import pytest
import re
from pathlib import Path


def test_no_direct_getenv_for_exposed_vars():
    """Fail if any exposed variable key is read directly via os.getenv or os.environ.get.

    This prevents import-time freezing of values and ensures all exposed keys
    go through the `config_registry` / variables engine.
    """
    try:
        from core import variables_engine as ve
    except Exception:
        pytest.skip("Could not import variables_engine; skipping static check")

    exposed_keys = list(ve.exposed_vars._defs.keys())
    if not exposed_keys:
        pytest.skip("No exposed vars registered; skipping")

    # Build regex to match os.getenv('KEY' or os.environ.get('KEY'") occurrences
    keys_pattern = "|".join(re.escape(k) for k in exposed_keys)
    getenv_re = re.compile(rf"os\.getenv\(\s*['\"]({keys_pattern})['\"]")
    environ_re = re.compile(rf"os\.environ\.get\(\s*['\"]({keys_pattern})['\"]")

    repo_root = Path(__file__).resolve().parents[1]
    py_files = list(repo_root.glob("**/*.py"))

    failures = []
    for p in py_files:
        # Skip virtual envs, site-packages, git metadata and test environments
        sp = str(p)
        if any(
            x in sp
            for x in (
                "/venv/",
                "/.venv",
                "/.venv_test",
                "/env/",
                "/site-packages/",
                "/.git/",
            )
        ):
            continue
        # Skip the variables_engine itself (it registers the vars)
        if p.name == "variables_engine.py":
            continue
        # Read file content
        try:
            txt = p.read_text()
        except Exception:
            continue
        m1 = getenv_re.search(txt)
        m2 = environ_re.search(txt)
        if m1:
            failures.append(f"{p}: os.getenv('{m1.group(1)}')")
        if m2:
            failures.append(f"{p}: os.environ.get('{m2.group(1)}')")

    if failures:
        msgs = "\n".join(failures[:20])
        pytest.fail(f"Direct getenv usage for exposed vars found:\n{msgs}\n")
