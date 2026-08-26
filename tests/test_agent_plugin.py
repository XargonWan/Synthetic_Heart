import pytest

import plugins.agent_plugin as agent_plugin_mod
from core.config_manager import config_registry
from plugins.agent_plugin import AgentPlugin


def test_is_enabled_reflects_toggle(monkeypatch):
    """The plugin must report its enabled state via is_enabled() so
    core_initializer skips registering the agent tools (and stops injecting
    them into prompts) when the agent is off.

    ``is_enabled()`` re-reads ``AGENT_ENABLED`` from the config registry on
    every call so WebUI toggles apply at runtime, so the test drives the
    toggle through the config lookup rather than the private attribute.
    """
    p = AgentPlugin()

    monkeypatch.setattr(config_registry, "get_var", lambda key, default=None: False)
    assert p.is_enabled() is False

    monkeypatch.setattr(config_registry, "get_var", lambda key, default=None: True)
    assert p.is_enabled() is True


def test_supported_actions_include_write_file():
    p = AgentPlugin()
    assert "agent_write_file" in p.get_supported_action_types()
    actions = p.get_supported_actions()
    spec = actions["agent_write_file"]
    assert spec["required_fields"] == ["path", "content"]
    # external_effects must be present so the router sends it to the Agent Lane.
    assert spec.get("external_effects")


@pytest.mark.asyncio
async def test_agent_write_file_roundtrip(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    target = "note.txt"
    res = await p.execute_action(
        {"type": "agent_write_file", "payload": {"path": target, "content": "hello"}},
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    written = (tmp_path / "note.txt").read_text(encoding="utf-8")
    assert written == "hello"

    # append mode extends the file rather than truncating it.
    res_append = await p.execute_action(
        {
            "type": "agent_write_file",
            "payload": {"path": target, "content": " world", "mode": "append"},
        },
        {},
        None,
        None,
    )
    assert res_append["status"] == "ok"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello world"


@pytest.mark.asyncio
async def test_agent_write_file_rejects_escape(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    res = await p.execute_action(
        {
            "type": "agent_write_file",
            "payload": {"path": "/etc/passwd", "content": "x"},
        },
        {},
        None,
        None,
    )
    assert res["status"] == "error"


def test_supported_actions_include_run_shell():
    p = AgentPlugin()
    assert "agent_run_shell" in p.get_supported_action_types()
    spec = p.get_supported_actions()["agent_run_shell"]
    assert spec["required_fields"] == ["command"]
    assert spec["security_level"] == "high"
    # shell effect routes it to the Agent Lane.
    assert "shell" in spec.get("external_effects", [])


@pytest.mark.asyncio
async def test_agent_run_shell_in_container_roundtrip(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])
    # Pretend we are inside a container so the gate opens.
    monkeypatch.setattr(agent_plugin_mod, "_is_in_container", lambda: True)

    res = await p.execute_action(
        {"type": "agent_run_shell", "payload": {"command": "echo hi"}},
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "hi"


@pytest.mark.asyncio
async def test_agent_run_shell_refused_on_host(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])
    # Not in a container and the host override is off -> must refuse.
    monkeypatch.setattr(agent_plugin_mod, "_is_in_container", lambda: False)
    monkeypatch.setattr(
        config_registry,
        "get_var",
        lambda key, default=None: False if key == "AGENT_SHELL_ALLOW_HOST" else default,
    )

    res = await p.execute_action(
        {"type": "agent_run_shell", "payload": {"command": "echo hi"}},
        {},
        None,
        None,
    )
    assert res["status"] == "error"
    assert "container" in res["reason"]


@pytest.mark.asyncio
async def test_agent_run_shell_rejects_cwd_escape(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])
    monkeypatch.setattr(agent_plugin_mod, "_is_in_container", lambda: True)

    res = await p.execute_action(
        {"type": "agent_run_shell", "payload": {"command": "ls", "cwd": "/etc"}},
        {},
        None,
        None,
    )
    assert res["status"] == "error"


def test_supported_actions_include_edit_and_search():
    p = AgentPlugin()
    types = p.get_supported_action_types()
    assert "agent_edit_file" in types
    assert "agent_search_files" in types

    edit_spec = p.get_supported_actions()["agent_edit_file"]
    assert edit_spec["required_fields"] == ["path", "old_string", "new_string"]
    assert edit_spec["security_level"] == "medium"
    # filesystem effect routes it to the Agent Lane.
    assert "filesystem" in edit_spec.get("external_effects", [])

    search_spec = p.get_supported_actions()["agent_search_files"]
    assert search_spec["required_fields"] == ["pattern"]


@pytest.mark.asyncio
async def test_agent_edit_file_roundtrip(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    (tmp_path / "code.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    res = await p.execute_action(
        {
            "type": "agent_edit_file",
            "payload": {
                "path": "code.py",
                "old_string": "y = 2",
                "new_string": "y = 3",
            },
        },
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    assert res["replacements"] == 1
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 1\ny = 3\n"


@pytest.mark.asyncio
async def test_agent_edit_file_ambiguous_match(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    (tmp_path / "dup.txt").write_text("a\na\n", encoding="utf-8")

    # Two occurrences but default expected_replacements=1 -> error.
    res = await p.execute_action(
        {
            "type": "agent_edit_file",
            "payload": {"path": "dup.txt", "old_string": "a", "new_string": "b"},
        },
        {},
        None,
        None,
    )
    assert res["status"] == "error"

    # With expected_replacements=2 the edit succeeds.
    res_ok = await p.execute_action(
        {
            "type": "agent_edit_file",
            "payload": {
                "path": "dup.txt",
                "old_string": "a",
                "new_string": "b",
                "expected_replacements": 2,
            },
        },
        {},
        None,
        None,
    )
    assert res_ok["status"] == "ok"
    assert res_ok["replacements"] == 2
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "b\nb\n"


@pytest.mark.asyncio
async def test_agent_edit_file_rejects_escape(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    res = await p.execute_action(
        {
            "type": "agent_edit_file",
            "payload": {
                "path": "/etc/passwd",
                "old_string": "root",
                "new_string": "x",
            },
        },
        {},
        None,
        None,
    )
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_agent_search_files_plain(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    (tmp_path / "a.py").write_text("import os\nprint('hello')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('world')\n", encoding="utf-8")

    res = await p.execute_action(
        {"type": "agent_search_files", "payload": {"pattern": "print", "glob": "*.py"}},
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    assert res["count"] == 2
    lines = {(m["path"], m["line"]) for m in res["matches"]}
    assert (str(tmp_path / "a.py"), 2) in lines
    assert (str(tmp_path / "b.py"), 1) in lines


@pytest.mark.asyncio
async def test_agent_search_files_regex_and_case(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    (tmp_path / "c.txt").write_text("Foo\nbar\nFOOBAR\n", encoding="utf-8")

    # Case-insensitive regex matches all three Foo/FOO lines.
    res = await p.execute_action(
        {
            "type": "agent_search_files",
            "payload": {"pattern": r"foo", "regex": True},
        },
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    assert res["count"] == 2

    # Case-sensitive plain search only matches the exact "Foo".
    res_cs = await p.execute_action(
        {
            "type": "agent_search_files",
            "payload": {"pattern": "Foo", "case_sensitive": True},
        },
        {},
        None,
        None,
    )
    assert res_cs["status"] == "ok"
    assert res_cs["count"] == 1


@pytest.mark.asyncio
async def test_agent_search_files_rejects_escape(monkeypatch, tmp_path):
    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])

    res = await p.execute_action(
        {"type": "agent_search_files", "payload": {"pattern": "x", "path": "/etc"}},
        {},
        None,
        None,
    )
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_agent_read_file_extracts_pdf_text(monkeypatch, tmp_path):
    """agent_read_file on a PDF returns extracted text, not raw PDF markup
    (the garbage that made the agent loop re-read attachments forever)."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (Hello rainbow test) Tj ET")
    page[NameObject("/Contents")] = content
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    writer.add_page(page)
    pdf_path = tmp_path / "test.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)

    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])
    res = await p.execute_action(
        {"type": "agent_read_file", "payload": {"path": str(pdf_path)}},
        {},
        None,
        None,
    )
    assert res["status"] == "ok"
    assert res["extracted"] == "pdf_text"
    assert "Hello rainbow test" in res["content"]


@pytest.mark.asyncio
async def test_agent_read_file_pdf_failure_returns_error(monkeypatch, tmp_path):
    """A broken/corrupt PDF yields a clear error, never raw binary garbage."""
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%% this is not a real pdf\n")

    p = AgentPlugin()
    monkeypatch.setattr(p, "_allowed_roots", lambda: [tmp_path])
    res = await p.execute_action(
        {"type": "agent_read_file", "payload": {"path": str(pdf_path)}},
        {},
        None,
        None,
    )
    assert res["status"] == "error"
    assert "pdf" in res["reason"].lower()


@pytest.mark.asyncio
async def test_agent_wait_clamps_and_sleeps(monkeypatch):
    """agent_wait paces polls: seconds clamped to [1, 60], actual sleep is
    awaited exactly once with the clamped value."""
    p = AgentPlugin()

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(agent_plugin_mod.asyncio, "sleep", fake_sleep)

    result = await p.execute_action(
        {"type": "agent_wait", "payload": {"seconds": 99}}, {}, None, None
    )
    assert result["status"] == "ok"
    assert result["seconds"] == 60
    assert sleeps == [60.0]

    await p.execute_action({"type": "agent_wait", "payload": {"seconds": 0}}, {}, None, None)
    assert sleeps[-1] == 1.0

    # No seconds given -> default 5.
    await p.execute_action({"type": "agent_wait", "payload": {}}, {}, None, None)
    assert sleeps[-1] == 5.0


def test_agent_wait_in_supported_actions():
    p = AgentPlugin()
    spec = p.get_supported_actions()["agent_wait"]
    assert spec["optional_fields"] == ["seconds"]
    # No external effects: waiting is pure pacing, it must not affect routing.
    assert not spec.get("external_effects")
