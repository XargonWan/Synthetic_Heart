import os

BASE = os.path.join(os.path.dirname(__file__), "..", "core", "webui_templates")

SECTIONS = [
    "home",
    "skins",
    "history",
    "logs",
    "settings",
    "components",
    "about",
    "agent",
]


def test_section_files_exist():
    for s in SECTIONS:
        path = os.path.join(BASE, "sections", f"{s}.html")
        assert os.path.exists(path), f"Missing section template: {path}"
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        assert len(data.strip()) > 0, f"Section file {s} appears empty"
