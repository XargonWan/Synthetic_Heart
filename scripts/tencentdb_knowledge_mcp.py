"""Run TencentDB Agent Memory's local Knowledge Service and MCP bridge.

This is developer tooling only. It is intentionally separate from SyntH's
runtime MCP registry in ``config/synth_mcp.json``.

The launcher starts the HTTP Knowledge Service on demand, waits for its health
endpoint, and then hands the current stdio streams to the upstream Knowledge
MCP server. If another launcher already owns a healthy service, it reuses it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = ROOT / ".tools" / "tencentdb-agent-memory" / "MemoryKnowledge"
NODE_MODULES = KNOWLEDGE_ROOT / "node_modules"
TSX_CLI = NODE_MODULES / "tsx" / "dist" / "cli.mjs"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _npm_node_command() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for TencentDB Knowledge Service")
    return node


def _service_url() -> str:
    return _env("TDAI_KNOWLEDGE_API_URL", "http://127.0.0.1:8421").rstrip("/")


def _healthy(url: str) -> bool:
    try:
        with urlopen(f"{url}/health", timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PORT", "8421")
    env.setdefault("KNOWLEDGE_DATA_DIR", str(KNOWLEDGE_ROOT / "data"))
    env.setdefault("KNOWLEDGE_DB_PATH", str(KNOWLEDGE_ROOT / "data" / "knowledge.db"))
    env.setdefault("KNOWLEDGE_PUBLIC_BASE_URL", f"{_service_url()}/v3")
    env.setdefault("TDAI_SERVICE_ID", "synthetic-heart-dev")
    env.setdefault("TDAI_CODE_GRAPH_ID", "cg-p22kpkhl")
    env.setdefault("TDAI_WIKI_ID", "wiki-7kop5yzt")
    env.setdefault("LOG_LEVEL", "info")

    # Optional BYO OpenAI-compatible configuration. Nothing is read from or
    # printed from the repository's .env file; secrets stay process-owned.
    if env.get("TDAI_KNOWLEDGE_LLM_API_KEY"):
        env.setdefault("LLM_MODE", "custom")
        env.setdefault("LLM_API_KEY", env["TDAI_KNOWLEDGE_LLM_API_KEY"])
        env.setdefault("LLM_BASE_URL", _env("TDAI_KNOWLEDGE_LLM_BASE_URL", ""))
        env.setdefault("LLM_MODEL", _env("TDAI_KNOWLEDGE_LLM_MODEL", "gpt-4o-mini"))

    return env


def _start_service(node: str, env: dict[str, str]) -> subprocess.Popen[str]:
    if not TSX_CLI.exists():
        raise RuntimeError(
            f"TencentDB dependencies are missing: {TSX_CLI}. "
            "Run npm install in .tools/tencentdb-agent-memory/MemoryKnowledge."
        )

    log_dir = KNOWLEDGE_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "knowledge-service.stdout.log").open("a", encoding="utf-8")
    stderr = (log_dir / "knowledge-service.stderr.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [node, str(TSX_CLI), "workspace-entry.ts"],
        cwd=KNOWLEDGE_ROOT,
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    # Keep the file handles alive through the child lifetime.
    process._tdai_log_handles = (stdout, stderr)  # type: ignore[attr-defined]
    return process


def _wait_for_service(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + float(_env("TDAI_KNOWLEDGE_START_TIMEOUT_SEC", "45"))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "TencentDB Knowledge Service exited during startup; see "
                f"{KNOWLEDGE_ROOT / 'logs' / 'knowledge-service.stderr.log'}"
            )
        if _healthy(url):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for TencentDB Knowledge Service at {url}")


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    finally:
        for handle in getattr(process, "_tdai_log_handles", ()):
            handle.close()


def main() -> int:
    if not KNOWLEDGE_ROOT.exists():
        print(
            "TencentDB Agent Memory is not checked out at "
            f"{KNOWLEDGE_ROOT}",
            file=sys.stderr,
        )
        return 1

    node = _npm_node_command()
    url = _service_url()
    service: subprocess.Popen[str] | None = None
    owns_service = not _healthy(url)

    try:
        if owns_service:
            service = _start_service(node, _build_env())
            _wait_for_service(url, service)

        # The upstream MCP server must own stdout/stdin for JSON-RPC. Invoke
        # tsx directly instead of `npm run` so npm banners cannot corrupt it.
        mcp = subprocess.Popen(
            [node, str(TSX_CLI), "workspace-mcp-entry.ts"],
            cwd=KNOWLEDGE_ROOT,
            env=_build_env(),
        )
        return mcp.wait()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"TencentDB Knowledge MCP failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_service:
            _stop(service)


if __name__ == "__main__":
    raise SystemExit(main())
