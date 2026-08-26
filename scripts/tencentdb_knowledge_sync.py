"""Provision and refresh the developer Wiki used by D16 coding agents.

This deliberately writes a curated set of existing SyntH documents directly as
locked Wiki pages. It does not invoke an LLM, so running it is deterministic
and does not incur inference cost. Usage::

    uv run python scripts/tencentdb_knowledge_sync.py

The Knowledge Service must be running; launching the configured MCP server
once is enough to start it, or use the Swagger UI at http://127.0.0.1:8421.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("TDAI_KNOWLEDGE_API_URL", "http://127.0.0.1:8421").rstrip("/")
SERVICE_ID = os.environ.get("TDAI_SERVICE_ID", "synthetic-heart-dev")
TEAM_ID = os.environ.get("TDAI_TEAM_ID", "synth-development")
WIKI_NAME = os.environ.get("TDAI_WIKI_NAME", "Synthetic Heart Developer Knowledge")

CURATED_FILES = [
    "AGENTS.md",
    "docs/architecture.rst",
    "docs/agentic_tools.rst",
    "docs/agent_integration.rst",
    "docs/action_schema_format.rst",
    "docs/component_pattern.rst",
    "docs/COMPONENT_DEVELOPMENT_GUIDE.rst",
    "docs/interfaces.rst",
    "docs/cortex.rst",
    "docs/database_connection_management.rst",
    "docs/rift_vessel.rst",
    "docs/prompt_pipeline.rst",
    "docs/prompt_engine_json_prompt.rst",
    "docs/memory_search_and_management.rst",
    "docs/wiki/en/content/Plugin System/Plugin Architecture & Design.md",
    "docs/wiki/en/content/Agent Core System/Agent Core System.md",
    "docs/wiki/en/content/Agent Core System/Action Execution Engine.md",
    "docs/wiki/en/content/Agent Core System/Context Management.md",
    "docs/wiki/en/content/Agent Core System/Message Processing Pipeline.md",
]


def request(path: str, method: str, payload: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "x-tdai-service-id": SERVICE_ID,
    }
    raw = json.dumps(payload).encode("utf-8")
    req = Request(f"{BASE_URL}/v3{path}", data=raw, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(
            f"Knowledge Service request failed for {path}: {exc}; {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Knowledge Service request failed for {path}: {exc}") from exc
    if result.get("code") not in (0, None):
        raise RuntimeError(f"Knowledge Service rejected {path}: {result}")
    return result


def ensure_wiki() -> str:
    result = request(
        "/wiki/create",
        "POST",
        {"team_id": TEAM_ID, "name": WIKI_NAME},
    )
    wiki_id = result.get("data", {}).get("wiki_id")
    if not wiki_id:
        raise RuntimeError(f"Wiki creation returned no wiki_id: {result}")
    return str(wiki_id)


def page_ref(source: str) -> str:
    # Stable refs make this command idempotent and keep provenance visible.
    return source.replace("\\", "/")


def sync_pages(wiki_id: str) -> int:
    pages = []
    for source in CURATED_FILES:
        path = ROOT / Path(source)
        if not path.exists():
            print(f"skip missing: {source}", file=sys.stderr)
            continue
        content = path.read_text(encoding="utf-8")
        pages.append({"ref": page_ref(source), "content": content})

    # The upstream API accepts at most 20 pages per write, matching the curated
    # set above. Keep batching here in case the list grows later.
    for offset in range(0, len(pages), 20):
        request(
            "/wiki/page/write",
            "POST",
            {
                "team_id": TEAM_ID,
                "wiki_id": wiki_id,
                "pages": pages[offset : offset + 20],
            },
        )
    return len(pages)


def main() -> int:
    try:
        wiki_id = ensure_wiki()
        count = sync_pages(wiki_id)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({"wiki_id": wiki_id, "pages_synced": count}, indent=2))
    print(
        "Persist these IDs for agents: "
        f"wiki_id={wiki_id}, service_id={SERVICE_ID}, team_id={TEAM_ID}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
