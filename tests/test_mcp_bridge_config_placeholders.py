"""Cross-platform path resolution for the Synth runtime MCP registry.

``config/synth_mcp.json`` is a single file shared by every deployment of this
repository, and those deployments do not share a filesystem layout: the reference
Docker image runs from ``/app`` with its interpreter at ``/app/venv/bin/python``,
while a Windows checkout has ``.venv\\Scripts\\python.exe``. A literal path is
therefore correct in at most one of them, and in the other the server dies at
spawn with a bare ``[WinError 2] The system cannot find the file specified`` that
never names the path it could not find — which is what happened at every startup
before these placeholders existed.

The tests cover the resolution itself, then prove the end result by spawning the
logs server from the real config file and calling a tool on it.

The end-to-end test speaks the MCP stdio transport directly rather than importing
the ``mcp`` client package: importing that package inside pytest trips over a
pre-existing namespace-package shadow from a bundled ``node_modules/dotenv``, and
the spawn plus a real ``tools/list`` / ``tools/call`` exchange is the stronger
proof anyway.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import core.mcp_bridge.config as config_module
from core.logging_utils import get_log_dir
from core.mcp_bridge.config import (
    expand_placeholders,
    load_enabled_synth_mcp_servers,
    load_synth_mcp_servers,
    repo_root,
)


def test_the_shipped_config_resolves_to_this_process_and_checkout() -> None:
    """The real ``config/synth_mcp.json`` must be runnable exactly as shipped.

    This is the regression guard for the Windows breakage: no shipped entry may
    depend on a path that is only valid in the container.
    """
    servers = load_enabled_synth_mcp_servers()

    logs = servers["synth_logs"]
    assert logs.command == sys.executable, (
        "command must be the interpreter running Synth"
    )
    assert [Path(a) for a in logs.args] == [
        repo_root() / "mcp_servers" / "synth_logs.py"
    ]
    # The server resolves LOG_DIR itself when it is absent, so this only has to be
    # a directory this process would really read logs from.
    assert logs.env["LOG_DIR"] == get_log_dir()

    failures = servers["synth_llm_failures"]
    assert failures.command == sys.executable
    assert [Path(a) for a in failures.args] == [
        repo_root() / "mcp_servers" / "synth_llm_failures.py"
    ]


def test_no_enabled_server_hardcodes_a_container_path() -> None:
    """A literal ``/app`` or ``/videodrome`` command is the bug, not a config."""
    for name, cfg in load_synth_mcp_servers().items():
        if not cfg.enabled:
            continue
        command = cfg.command or ""
        assert not command.startswith(("/app", "/videodrome")), (
            f"{name} still hardcodes a container path as its command: {command!r}"
        )
        for arg in cfg.args:
            assert not str(arg).startswith(("/app", "/videodrome")), (
                f"{name} still hardcodes a container path as an argument: {arg!r}"
            )


def test_literal_paths_are_used_as_written(tmp_path: Path) -> None:
    """A deployment that wants literal paths keeps them, unchanged."""
    cfg_file = tmp_path / "synth_mcp.json"
    cfg_file.write_text(
        json.dumps(
            {
                "synthMcpServers": {
                    "literal": {
                        "enabled": True,
                        "command": "/opt/custom/python",
                        "args": ["/opt/custom/server.py"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_synth_mcp_servers(cfg_file)["literal"]

    assert cfg.command == "/opt/custom/python"
    assert cfg.args == ["/opt/custom/server.py"]


def test_environment_references_expand_with_and_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTH_TEST_ROOT", "/srv/checkout")
    monkeypatch.delenv("SYNTH_TEST_ABSENT", raising=False)

    assert expand_placeholders("${SYNTH_TEST_ROOT}/logs") == "/srv/checkout/logs"
    assert expand_placeholders("${SYNTH_TEST_ROOT}") == "/srv/checkout"
    # Unset with an explicit default
    assert expand_placeholders("${SYNTH_TEST_ABSENT:-/app}") == "/app"
    # Unset with no default follows the shell convention: empty
    assert expand_placeholders("a${SYNTH_TEST_ABSENT}b") == "ab"
    # A default may itself contain a placeholder, and both passes apply
    assert expand_placeholders("${SYNTH_TEST_ABSENT:-{repo_root}}") == str(repo_root())


def test_unknown_placeholder_is_left_in_place_and_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not become a silently broken command.

    The whole reason this work happened is that a wrong path failed with an error
    that never named the path. An unknown placeholder is therefore left visible
    and reported, rather than quietly dropped.
    """
    warnings: list[str] = []
    monkeypatch.setattr(
        config_module, "log_warning", lambda msg, *a, **k: warnings.append(str(msg))
    )

    cfg_file = tmp_path / "synth_mcp.json"
    cfg_file.write_text(
        json.dumps(
            {
                "synthMcpServers": {
                    "typo": {"enabled": True, "command": "{pyton}", "args": []}
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_synth_mcp_servers(cfg_file)["typo"]

    assert cfg.command == "{pyton}", "an unknown placeholder must survive visibly"
    assert any("pyton" in w for w in warnings), (
        f"the unknown placeholder must be named in a warning; got {warnings!r}"
    )


def test_prose_fields_keep_their_braces(tmp_path: Path) -> None:
    """Only executed/exported fields are expanded, never descriptions."""
    cfg_file = tmp_path / "synth_mcp.json"
    cfg_file.write_text(
        json.dumps(
            {
                "synthMcpServers": {
                    "prose": {
                        "enabled": True,
                        "command": "{python}",
                        "args": [],
                        "description": "returns {repo_root} and {not_a_token} verbatim",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_synth_mcp_servers(cfg_file)["prose"]

    assert cfg.description == "returns {repo_root} and {not_a_token} verbatim"


def test_disabled_servers_are_loaded_but_not_enabled(tmp_path: Path) -> None:
    cfg_file = tmp_path / "synth_mcp.json"
    cfg_file.write_text(
        json.dumps({"synthMcpServers": {"off": {"enabled": False, "command": "npx"}}}),
        encoding="utf-8",
    )

    assert "off" in load_synth_mcp_servers(cfg_file)
    assert "off" not in load_enabled_synth_mcp_servers(cfg_file)


# ---------------------------------------------------------------------------
# End-to-end: the shipped config must actually spawn a working server
# ---------------------------------------------------------------------------


class _StdioRpc:
    """Minimal MCP stdio client: newline-delimited JSON-RPC, no extra deps."""

    def __init__(
        self, command: str, args: list[str], cwd: Path, env: dict[str, str]
    ) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [command, *args],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read_response(self, request_id: int, timeout: float = 30.0) -> dict:
        """Read until the response carrying ``request_id`` arrives."""
        deadline = timeout
        while deadline > 0:
            try:
                line = self._lines.get(timeout=deadline)
            except queue.Empty:
                break
            deadline -= 1
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # server chatter on stdout
            if payload.get("id") == request_id:
                return payload
        stderr = ""
        if self.proc.stderr is not None:
            stderr = self.proc.stderr.read()[-800:]
        raise AssertionError(
            f"no response for request {request_id}; server stderr tail: {stderr!r}"
        )

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def test_the_real_config_actually_spawns_the_logs_server() -> None:
    """Read the shipped config, spawn the server, list tools, call one.

    This single test would have caught the original breakage: a container-only
    interpreter path fails here as ``WinError 2`` on Windows.
    """
    cfg = load_enabled_synth_mcp_servers()["synth_logs"]

    # LOG_DIR is pinned to this checkout's own logs directory so the call has
    # something deterministic to report; the registry's own value is asserted
    # separately above.
    env = {**os.environ, **cfg.env, "LOG_DIR": str(repo_root() / "logs")}

    rpc = _StdioRpc(
        command=cfg.command or "",
        args=list(cfg.args),
        cwd=repo_root(),
        env=env,
    )
    try:
        rpc.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "config-placeholder-test", "version": "0"},
                },
            }
        )
        init = rpc.read_response(1)
        assert "result" in init, f"initialize failed: {init}"
        assert init["result"]["serverInfo"]["name"]

        rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        rpc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = rpc.read_response(2)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {
            "list_log_files",
            "search_logs",
            "tail_log",
            "get_recent_errors",
        } <= names

        rpc.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_log_files", "arguments": {}},
            }
        )
        called = rpc.read_response(3)
        text = "".join(part.get("text", "") for part in called["result"]["content"])
        # It read the real log directory, so the answer names real files. The
        # tool lists log *stems* (synth, cortex_api, …), each with its backups.
        assert "Log directory:" in text, f"unexpected output: {text[:400]!r}"
        assert str(repo_root() / "logs") in text
        listed_names = {line.split()[0] for line in text.splitlines() if line.split()}
        assert any(name.startswith("synth") for name in listed_names), (
            f"the real synth logs are not listed: {sorted(listed_names)[:12]!r}"
        )
    finally:
        rpc.close()
