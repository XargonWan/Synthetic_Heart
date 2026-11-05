"""SynthUtils.escapeHtml must be safe inside HTML attribute values too."""

import shutil
import subprocess
from pathlib import Path

import pytest

UTILS_JS = Path(__file__).resolve().parents[1] / "res/synth_webui/js/utils.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")


def _escape(value: str) -> str:
    script = (
        "global.window = {}; global.document = {};"
        f"require({str(UTILS_JS)!r});"
        "process.stdout.write(window.SynthUtils.escapeHtml(process.argv[1]));"
    )
    return subprocess.run(
        ["node", "-e", script, value], capture_output=True, text=True, check=True
    ).stdout


def test_quotes_and_markup_are_encoded() -> None:
    out = _escape('" onmouseover="alert(1)" <b>&\'')
    assert '"' not in out and "'" not in out and "<" not in out
    assert out == "&quot; onmouseover=&quot;alert(1)&quot; &lt;b&gt;&amp;&#39;"


def test_nullish_values_become_empty() -> None:
    script = (
        "global.window = {}; global.document = {};"
        f"require({str(UTILS_JS)!r});"
        "process.stdout.write(window.SynthUtils.escapeHtml(null)+window.SynthUtils.escapeHtml(undefined));"
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == ""
