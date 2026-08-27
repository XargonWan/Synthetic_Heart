import pytest

from plugins.document_skill.document_skill import (
    DocumentSkillPlugin,
    _NUMBERED_HEADING_RE,
)


@pytest.fixture
def plugin():
    return DocumentSkillPlugin()


def test_numbered_heading_regex():
    assert _NUMBERED_HEADING_RE.match("1. Introduction")
    assert _NUMBERED_HEADING_RE.match("1.2 The Plan")
    assert _NUMBERED_HEADING_RE.match("12) Results")
    assert _NUMBERED_HEADING_RE.match("3 - Chapter Three")
    assert not _NUMBERED_HEADING_RE.match("Once upon a time")
    assert not _NUMBERED_HEADING_RE.match("1234567890 long line")


def test_detect_sections_numbered_headings(plugin):
    text = (
        "1. Introduction\n"
        "This is the intro body.\n"
        "\n"
        "2. Methods\n"
        "We did things.\n"
        "\n"
        "3. Conclusion\n"
        "We finished.\n"
    )
    sections = plugin._detect_sections(text, max_sections=50)
    assert len(sections) == 3
    assert sections[0]["title"] == "1. Introduction"
    assert "intro body" in sections[0]["text"]
    assert sections[1]["title"] == "2. Methods"
    assert sections[2]["title"] == "3. Conclusion"


def test_detect_sections_short_standalone_lines(plugin):
    text = (
        "Prologue\n"
        "\n"
        "Some opening text here that is long enough to be body.\n"
        "\n"
        "Chapter One\n"
        "\n"
        "The actual chapter body text.\n"
    )
    sections = plugin._detect_sections(text, max_sections=50)
    assert len(sections) == 2
    assert sections[0]["title"] == "Prologue"
    assert sections[1]["title"] == "Chapter One"


def test_detect_sections_respects_max(plugin):
    text = "\n".join(f"{i}. Heading {i}\nbody {i}" for i in range(1, 20))
    sections = plugin._detect_sections(text, max_sections=5)
    assert len(sections) == 5


def test_resolve_safe_path_inside_root(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4 fake")
    resolved, err = plugin._resolve_safe_path("doc.pdf")
    assert err is None
    assert resolved == target.resolve()


def test_resolve_safe_path_outside_root(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    resolved, err = plugin._resolve_safe_path(str(outside))
    assert resolved is None
    assert "outside allowed roots" in (err or "")


def test_extract_unsupported_format(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    content, err = plugin._extract(txt, max_chars=1000, page_start=1, page_end=0)
    assert err and "unsupported" in err


def test_extract_docx(plugin, tmp_path, monkeypatch):
    docx = pytest.importorskip("docx")
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    target = tmp_path / "sample.docx"
    document = docx.Document()
    document.add_heading("1. First", level=1)
    document.add_paragraph("Body of first section.")
    document.add_heading("2. Second", level=1)
    document.add_paragraph("Body of second section.")
    document.save(str(target))

    content, err = plugin._extract(target, max_chars=5000, page_start=1, page_end=0)
    assert err == ""
    assert "1. First" in content
    assert "2. Second" in content


@pytest.mark.asyncio
async def test_execute_extract_text_missing_file(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    result = await plugin.execute_action(
        {"type": "document_extract_text", "payload": {"path": "nope.pdf"}},
        {},
        None,
        None,
    )
    assert result["status"] == "error"
    assert "not found" in result["reason"]


@pytest.mark.asyncio
async def test_execute_list_sections_docx(plugin, tmp_path, monkeypatch):
    docx = pytest.importorskip("docx")
    monkeypatch.setenv("AGENT_FS_ROOTS", str(tmp_path))
    target = tmp_path / "sample.docx"
    document = docx.Document()
    document.add_heading("1. Alpha", level=1)
    document.add_paragraph("Alpha body.")
    document.add_heading("2. Beta", level=1)
    document.add_paragraph("Beta body.")
    document.save(str(target))

    result = await plugin.execute_action(
        {"type": "document_list_sections", "payload": {"path": str(target)}},
        {},
        None,
        None,
    )
    assert result["status"] == "ok"
    assert result["count"] == 2
    titles = [s["title"] for s in result["sections"]]
    assert "1. Alpha" in titles
    assert "2. Beta" in titles
