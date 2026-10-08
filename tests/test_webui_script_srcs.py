"""Every /js/*.js <script> src in the WebUI templates must resolve to a real file.

Regression guard: res/synth_webui/js/skins.js was deleted while two templates
still referenced it, 404ing on every page load.
"""

import re
from pathlib import Path

TEMPLATES = [
    Path("core/webui_templates/synth_webui_shell.html"),
    Path("core/webui_templates/synth_webui_index.html"),
]
JS_ROOT = Path("res/synth_webui/js")


def test_template_script_srcs_exist():
    missing = []
    for tpl in TEMPLATES:
        for src in re.findall(
            r'<script\s+src="(/js/[^"]+)"', tpl.read_text(encoding="utf-8")
        ):
            path = src.split("?", 1)[0]
            assert path.startswith("/js/"), path
            if not (JS_ROOT / path[len("/js/") :]).exists():
                missing.append(f"{tpl.name}: {src}")
    assert not missing, f"dangling <script> srcs: {missing}"


def test_three_version_unified():
    # Every pinned three.js must resolve to ONE version: the importmaps feed
    # bare 'three' imports inside three-vrm, while our modules pin direct
    # URLs. A split (e.g. 0.169 vs 0.160) loads two Three instances and
    # three-vrm materials warn 'onBuild() has been removed' every compile.
    versions = set()
    for tpl in TEMPLATES:
        versions.update(
            re.findall(
                r"three@([0-9.]+)/build/three\.module\.js",
                tpl.read_text(encoding="utf-8"),
            )
        )
    for path in (
        list(JS_ROOT.glob("*.js"))
        + list(JS_ROOT.glob("*.mjs"))
        + [
            JS_ROOT / "README.md",
        ]
    ):
        versions.update(
            re.findall(
                r"three@([0-9.]+)/",
                path.read_text(encoding="utf-8", errors="ignore"),
            )
        )
    assert len(versions) == 1, f"split three.js versions pinned: {sorted(versions)}"
