"""The default install must work without the local voice stack installed.

``torch``, ``torchaudio``, ``kittentts`` and ``vosk`` are not Core dependencies: the local
voice and hearing engines are extensions that carry their own libraries. Nothing on the
base import path may need them.

These tests run in a subprocess with the four packages made unimportable, which
is the only faithful way to check "installed without them" while this checkout
still has them in its own environment.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = textwrap.dedent(
    """
    import importlib
    import sys

    BLOCKED = {"torch", "torchaudio", "kittentts", "vosk"}


    class _Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError("blocked for this test: " + fullname)
            return None


    sys.meta_path.insert(0, _Blocker())

    vad = importlib.import_module("core.vad_service")
    assert vad.VAD_SERVICE.initialize() is False, "VAD must degrade, not raise"

    leaked = sorted(BLOCKED & set(sys.modules))
    assert not leaked, "a blocked module was imported anyway: %s" % leaked

    print("LIGHT-PROFILE-OK")
    """
)


def _run_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )


def test_base_import_path_survives_without_local_voice() -> None:
    result = _run_probe()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LIGHT-PROFILE-OK" in result.stdout


def test_light_profile_probe_covers_the_dropped_packages() -> None:
    """Guard the guard: the probe must actually name every optional package."""
    for package in ("torch", "torchaudio", "kittentts", "vosk"):
        assert f'"{package}"' in _PROBE
