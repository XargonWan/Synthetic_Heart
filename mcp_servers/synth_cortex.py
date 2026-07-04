#!/usr/bin/env python3
"""MCP server for structured inspection of cortex_api.log.

Parses the banner-format log that cortex_api_logger.py writes and exposes
four focused tools that let agents audit prompt assembly and LLM behaviour
without loading the raw 6+ MB file into their context window.

Tools
-----
cortex_sessions       -- compact table of recent sessions with key metrics
cortex_read           -- full REQUEST + RESPONSE text for one session
cortex_analyze        -- structured breakdown: prompt composition + actions returned
cortex_search         -- find sessions whose payload or response match a keyword

Session IDs
-----------
1 = most recent session, 2 = second-most-recent, etc.
IDs shift as the log grows — always call cortex_sessions() first to get
current IDs then drill in with the other tools.

Usage (stdio transport, registered in .mcp.json)
-------------------------------------------------
    uv run python mcp_servers/synth_cortex.py
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_LOG_DIR = Path(os.getenv("LOG_DIR", str(_SCRIPT_DIR.parent / "logs")))
_LOG_FILE = _LOG_DIR / "cortex_api.log"

mcp = FastMCP("synth-cortex")

# ---------------------------------------------------------------------------
# Banner-format regex
# ---------------------------------------------------------------------------

_REQ_BANNER_RE = re.compile(r"REQUEST\s+\[(.+? UTC)\]\s+engine=(\S+)\s+model=(\S+)")
_RESP_BANNER_RE = re.compile(r"RESPONSE \[(.+? UTC)\]\s+(.*)")
_ELAPSED_RE = re.compile(r"elapsed=(\d+)ms")
_STATUS_RE = re.compile(r"status=(\d+)")
_TOKENS_RE = re.compile(r"tokens=\[(.+?)\]")
_PROMPT_TOK_RE = re.compile(r"prompt=(\d+)")
_COMPL_TOK_RE = re.compile(r"completion=(\d+)")
_CACHE_RE = re.compile(r"cache_read=(\d+)")

# Placeholder produced by sanitize_for_log: "<string: 42715 chars>"
_SANITIZED_STR_RE = re.compile(r"<string: (\d+) chars>")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Session:
    index: int = 0  # 1 = most recent
    # Request
    req_ts: str = ""
    engine: str = ""
    model: str = ""
    req_url: str = ""
    req_payload_text: str = ""
    # Response
    resp_ts: str = ""
    resp_elapsed_ms: int = 0
    resp_status: Optional[int] = None
    resp_prompt_tokens: Optional[int] = None
    resp_completion_tokens: Optional[int] = None
    resp_cache_read: Optional[int] = None
    resp_body_text: str = ""
    resp_error: str = ""


# ---------------------------------------------------------------------------
# File reader + parser
# ---------------------------------------------------------------------------


def _read_lines() -> list[str]:
    if not _LOG_FILE.exists():
        return []
    try:
        return _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _find_session_starts(lines: list[str]) -> list[int]:
    """Return line indices of every ════ line that introduces a REQUEST block."""
    starts: list[int] = []
    for i, line in enumerate(lines):
        if line.startswith("═") and i + 1 < len(lines):
            if lines[i + 1].strip().startswith("REQUEST "):
                starts.append(i)
    return starts


def _parse_one_session(lines: list[str], start: int, end: int) -> Session:
    """Parse a slice of lines (one session) into a Session object."""
    sess = Session()
    state = "req_header"  # req_header → req_body → resp_header → resp_body
    req_body: list[str] = []
    resp_body: list[str] = []

    for line in lines[start:end]:
        if state == "req_header":
            m = _REQ_BANNER_RE.search(line)
            if m:
                sess.req_ts = m.group(1)
                sess.engine = m.group(2)
                sess.model = m.group(3)
                state = "req_after_banner"

        elif state == "req_after_banner":
            # Skip the closing ════ separator after the REQUEST banner line.
            if line.startswith("═") or not line.strip():
                state = "req_body"
            else:
                state = "req_body"
                if line.startswith("URL:"):
                    sess.req_url = line[4:].strip()
                else:
                    req_body.append(line)

        elif state == "req_body":
            if line.startswith("─"):
                state = "resp_header"
            elif line.startswith("URL:"):
                sess.req_url = line[4:].strip()
            else:
                req_body.append(line)

        elif state == "resp_header":
            m = _RESP_BANNER_RE.search(line)
            if m:
                sess.resp_ts = m.group(1)
                rest = m.group(2)
                em = _ELAPSED_RE.search(rest)
                if em:
                    sess.resp_elapsed_ms = int(em.group(1))
                sm = _STATUS_RE.search(rest)
                if sm:
                    sess.resp_status = int(sm.group(1))
                tm = _TOKENS_RE.search(rest)
                if tm:
                    tok = tm.group(1)
                    p = _PROMPT_TOK_RE.search(tok)
                    if p:
                        sess.resp_prompt_tokens = int(p.group(1))
                    c = _COMPL_TOK_RE.search(tok)
                    if c:
                        sess.resp_completion_tokens = int(c.group(1))
                    cr = _CACHE_RE.search(tok)
                    if cr:
                        sess.resp_cache_read = int(cr.group(1))
                # Stay in resp_header — wait for the closing ─── separator
                # that follows the RESPONSE banner before switching to body.
                state = "resp_after_banner"
            elif not line.startswith("─") and line.strip():
                state = "resp_body"
                resp_body.append(line)

        elif state == "resp_after_banner":
            # The line after the RESPONSE banner is the closing ─── separator.
            # Consume it and move to body regardless of content.
            if line.startswith("─") or not line.strip():
                state = "resp_body"
            else:
                # No separator found — treat this line as body start
                state = "resp_body"
                resp_body.append(line)

        elif state == "resp_body":
            if line.startswith("ERROR:"):
                sess.resp_error = line[6:].strip()
            else:
                resp_body.append(line)

    sess.req_payload_text = "\n".join(ln for ln in req_body if ln.strip()).strip()
    sess.resp_body_text = "\n".join(resp_body).strip()
    return sess


def _load_sessions(limit: int = 100) -> list[Session]:
    """Load the most recent `limit` sessions, index 1 = most recent."""
    lines = _read_lines()
    if not lines:
        return []

    starts = _find_session_starts(lines)
    if not starts:
        return []

    # Take the last `limit` sessions
    starts = starts[-limit:]

    sessions: list[Session] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        sess = _parse_one_session(lines, start, end)
        sessions.append(sess)

    # Reverse so index 1 = most recent, then assign indices
    sessions.reverse()
    for i, sess in enumerate(sessions, 1):
        sess.index = i

    return sessions


# ---------------------------------------------------------------------------
# Prompt analysis helpers
# ---------------------------------------------------------------------------


def _try_json(text: str) -> Any:
    """Try JSON parse; return None on failure."""
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_payload_structure(payload_text: str) -> dict[str, Any]:
    """Parse what we can from the (possibly sanitized) payload JSON."""
    # Strip the "Payload:" label if present
    text = re.sub(r"^Payload:\s*", "", payload_text, flags=re.MULTILINE).strip()
    obj = _try_json(text)
    if not isinstance(obj, dict):
        # Try finding the opening brace
        idx = text.find("{")
        if idx >= 0:
            obj = _try_json(text[idx:])
    if not isinstance(obj, dict):
        return {"parse_error": "Could not parse payload JSON"}

    result: dict[str, Any] = {}

    # ── Gemini format ──────────────────────────────────────────────────────
    if "system_instruction" in obj or "contents" in obj:
        result["format"] = "gemini"

        si = obj.get("system_instruction", "")
        if isinstance(si, str):
            m = _SANITIZED_STR_RE.match(si)
            result["system_chars"] = int(m.group(1)) if m else len(si)
            if not m:
                result["system_preview"] = si[:300]
        elif isinstance(si, dict):
            parts = si.get("parts", [])
            text_part = next(
                (p.get("text", "") for p in parts if isinstance(p, dict)), ""
            )
            m = (
                _SANITIZED_STR_RE.match(text_part)
                if isinstance(text_part, str)
                else None
            )
            result["system_chars"] = int(m.group(1)) if m else len(str(text_part))

        contents = obj.get("contents", [])
        result["contents_turns"] = len(contents)

        # Look for context JSON in the user parts
        for turn in contents:
            if not isinstance(turn, dict):
                continue
            for part in turn.get("parts", []):
                if not isinstance(part, dict):
                    continue
                txt = part.get("text", "")
                if not isinstance(txt, str):
                    continue
                m = _SANITIZED_STR_RE.match(txt)
                if m:
                    result["user_content_chars"] = int(m.group(1))
                elif txt.strip().startswith("{"):
                    _parse_context_blob(result, txt)
                    break

        # Other Gemini fields
        if "response_mime_type" in obj:
            result["response_mime_type"] = obj["response_mime_type"]
        if "generation_config" in obj:
            gc = obj["generation_config"]
            if isinstance(gc, dict):
                result["generation_config"] = {
                    k: v
                    for k, v in gc.items()
                    if k in ("temperature", "max_output_tokens", "response_mime_type")
                }

    # ── OpenAI / OpenRouter format ─────────────────────────────────────────
    elif "messages" in obj:
        result["format"] = "openai"
        messages = obj.get("messages", [])
        result["messages_count"] = len(messages)
        if "model" in obj:
            result["api_model"] = obj["model"]

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    m = _SANITIZED_STR_RE.match(content)
                    result["system_chars"] = int(m.group(1)) if m else len(content)
                    if not m:
                        result["system_preview"] = content[:300]
            elif role == "user" and isinstance(content, str):
                m = _SANITIZED_STR_RE.match(content)
                if m:
                    result["user_content_chars"] = int(m.group(1))
                elif content.strip().startswith("{"):
                    _parse_context_blob(result, content)

        extra = {
            k: obj[k]
            for k in ("temperature", "max_tokens", "response_format", "stream")
            if k in obj
        }
        if extra:
            result["api_params"] = extra

    else:
        result["format"] = "unknown"
        result["top_level_keys"] = list(obj.keys())

    return result


def _parse_context_blob(result: dict[str, Any], text: str) -> None:
    """Parse the inner context JSON blob injected as a user-turn message."""
    obj = _try_json(text)
    if not isinstance(obj, dict):
        return

    # Correction context looks like: {"system_message": {...}}
    if "system_message" in obj:
        sm = obj["system_message"]
        result["is_correction"] = True
        if isinstance(sm, dict):
            result["correction_type"] = sm.get("type", "")
            result["correction_preview"] = str(sm.get("message", ""))[:250]
        return

    ctx = obj.get("context", obj)
    if not isinstance(ctx, dict):
        return

    result["context_keys"] = list(ctx.keys())

    def _count(key: str) -> int | str:
        v = ctx.get(key, [])
        return len(v) if isinstance(v, list) else "?"

    result["history_current_chat"] = _count("history_current_chat")
    result["history_recent"] = _count("history_recent")
    result["memories"] = _count("memories")
    result["thoughts"] = _count("thoughts")
    result["participants"] = _count("participants")
    result["tags"] = _count("tags_placeholder")

    es = ctx.get("emotion_state", "")
    if isinstance(es, str) and es:
        result["emotion_state"] = es[:200]

    chat = ctx.get("history_current_chat", [])
    if isinstance(chat, list) and chat:
        result["last_chat_msg"] = str(chat[-1])[:200]

    mems = ctx.get("memories", [])
    if isinstance(mems, list) and mems:
        result["memory_sample"] = [str(m)[:120] for m in mems[:3]]


_ACTION_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"]+)"')


def _parse_response_actions(body_text: str) -> list[dict[str, Any]]:
    """Try to extract the actions list from an LLM response body.

    Falls back to regex extraction when the body has been textwrap'd
    (which breaks JSON validity by inserting unescaped newlines inside strings).
    """
    # Attempt 1: valid JSON parse
    obj = _try_json(body_text)
    if isinstance(obj, dict):
        # Direct actions list
        actions = obj.get("actions", [])
        if isinstance(actions, list) and actions:
            return [
                {
                    "type": a.get("type", "?") if isinstance(a, dict) else "?",
                    "keys": list(a.keys()) if isinstance(a, dict) else [],
                }
                for a in actions[:20]
            ]
        # Gemini native SDK response: actions inside candidates → parts → text
        for cand in obj.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                txt = part.get("text", "")
                if isinstance(txt, str):
                    inner = _try_json(txt)
                    if isinstance(inner, dict) and "actions" in inner:
                        return _parse_response_actions(txt)

    # Attempt 2: regex fallback (handles textwrap-damaged JSON)
    types = _ACTION_TYPE_RE.findall(body_text)
    if types:
        return [{"type": t, "keys": ["(regex)"]} for t in types[:20]]

    return []


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def cortex_sessions(limit: int = 20, since_minutes: Optional[int] = None) -> str:
    """Compact table of recent LLM sessions with key metrics.

    Each row shows: session ID, timestamp, engine/model, elapsed time,
    token counts (prompt / completion / cache_read), and a flag for
    corrections or errors.

    Use the session ID with cortex_read() or cortex_analyze() to drill in.

    Args:
        limit:         Max sessions to return (default 20, max 200).
        since_minutes: If set, only show sessions from the last N minutes
                       (based on request timestamp).
    """
    limit = min(max(1, limit), 200)
    sessions = _load_sessions(limit=limit)

    if since_minutes is not None:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=since_minutes)

        def _ts_dt(ts: str) -> Optional[datetime]:
            try:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                return None

        sessions = [
            s for s in sessions if (dt := _ts_dt(s.req_ts)) is not None and dt >= cutoff
        ]

    if not sessions:
        return "No cortex sessions found."

    header = (
        f"{'ID':>4}  {'TIMESTAMP (UTC)':<22}  {'ENGINE':<22}  {'MODEL':<30}"
        f"  {'ELAP':>6}  {'PTOK':>6}  {'CTOK':>5}  {'CR':>6}  FLAGS"
    )
    sep = "─" * len(header)
    rows = [header, sep]

    for s in sessions:
        ts = s.req_ts.replace(" UTC", "")
        elapsed = f"{s.resp_elapsed_ms // 1000}s" if s.resp_elapsed_ms else "  ?"
        ptok = str(s.resp_prompt_tokens) if s.resp_prompt_tokens is not None else "?"
        ctok = (
            str(s.resp_completion_tokens)
            if s.resp_completion_tokens is not None
            else "?"
        )
        cr = str(s.resp_cache_read) if s.resp_cache_read else "-"

        flags: list[str] = []
        if s.resp_error:
            flags.append("ERR")
        # Peek for correction marker
        if (
            '"system_message"' in s.req_payload_text
            or "CORRECTION" in s.req_payload_text
        ):
            flags.append("CORR")
        if s.resp_status and s.resp_status >= 400:
            flags.append(f"HTTP{s.resp_status}")

        model_short = s.model[:30]
        rows.append(
            f"{s.index:>4}  {ts:<22}  {s.engine:<22}  {model_short:<30}"
            f"  {elapsed:>6}  {ptok:>6}  {ctok:>5}  {cr:>6}  {' '.join(flags)}"
        )

    rows.append(
        f"\n{len(sessions)} session(s). Use cortex_read(N) or cortex_analyze(N) to drill in."
    )
    return "\n".join(rows)


@mcp.tool()
def cortex_read(session_id: int, max_chars: int = 8000) -> str:
    """Return the raw REQUEST + RESPONSE text for a session.

    The payload section is the sanitized version already in the log
    (large strings appear as '<string: N chars>').  The response body
    is the full LLM output.

    Args:
        session_id: 1-based session index from cortex_sessions().
        max_chars:  Cap on total characters returned (default 8000).
                    Increase if you need to read a complete large response.
    """
    sessions = _load_sessions(limit=max(session_id + 10, 50))
    sess = next((s for s in sessions if s.index == session_id), None)
    if sess is None:
        return f"Session {session_id} not found. Call cortex_sessions() to see available IDs."

    lines: list[str] = [
        f"═══ SESSION {session_id} ═══",
        f"REQUEST  [{sess.req_ts}]  engine={sess.engine}  model={sess.model}",
    ]
    if sess.req_url:
        lines.append(f"URL: {sess.req_url}")
    lines.append("")
    lines.append("PAYLOAD:")
    lines.append(sess.req_payload_text or "(empty)")
    lines.append("")
    lines.append(
        f"RESPONSE [{sess.resp_ts}]  elapsed={sess.resp_elapsed_ms}ms"
        + (f"  status={sess.resp_status}" if sess.resp_status else "")
        + (
            f"  tokens=[prompt={sess.resp_prompt_tokens} completion={sess.resp_completion_tokens}"
            + (f" cache_read={sess.resp_cache_read}" if sess.resp_cache_read else "")
            + "]"
            if sess.resp_prompt_tokens is not None
            else ""
        )
    )
    if sess.resp_error:
        lines.append(f"ERROR: {sess.resp_error}")
    lines.append("")
    lines.append(sess.resp_body_text or "(empty)")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = (
            text[:max_chars]
            + f"\n\n... [truncated at {max_chars} chars — increase max_chars to read more]"
        )
    return text


@mcp.tool()
def cortex_analyze(session_id: int) -> str:
    """Structured breakdown of prompt composition and LLM actions for a session.

    Shows:
    - Prompt format (gemini / openai) and key sizing info
    - Context components: system chars, history counts, memory count,
      emotion state, correction flag
    - Actions returned by the LLM (type list, not full payloads)
    - Token efficiency metrics

    This is the primary tool for diagnosing prompt assembly issues —
    call cortex_sessions() first to find the session ID.

    Args:
        session_id: 1-based session index from cortex_sessions().
    """
    sessions = _load_sessions(limit=max(session_id + 10, 50))
    sess = next((s for s in sessions if s.index == session_id), None)
    if sess is None:
        return f"Session {session_id} not found. Call cortex_sessions() to see available IDs."

    lines: list[str] = [
        f"╔═══ ANALYSIS: Session {session_id} ═══╗",
        f"  Timestamp : {sess.req_ts}",
        f"  Engine    : {sess.engine}",
        f"  Model     : {sess.model}",
        f"  Elapsed   : {sess.resp_elapsed_ms}ms"
        + (f"  ({sess.resp_elapsed_ms / 1000:.1f}s)" if sess.resp_elapsed_ms else ""),
    ]

    if sess.resp_prompt_tokens is not None:
        tok = f"  Tokens    : prompt={sess.resp_prompt_tokens}"
        if sess.resp_completion_tokens is not None:
            tok += f"  completion={sess.resp_completion_tokens}"
        if sess.resp_cache_read:
            tok += f"  cache_read={sess.resp_cache_read}"
            pct = round(100 * sess.resp_cache_read / sess.resp_prompt_tokens)
            tok += f"  ({pct}% cached)"
        lines.append(tok)

    lines.append("")
    lines.append("── PROMPT STRUCTURE ──")

    payload_info = _extract_payload_structure(sess.req_payload_text)
    if "parse_error" in payload_info:
        lines.append(f"  Could not parse payload: {payload_info['parse_error']}")
    else:
        fmt = payload_info.get("format", "unknown")
        lines.append(f"  Format          : {fmt}")

        if "system_chars" in payload_info:
            lines.append(f"  System prompt   : {payload_info['system_chars']:,} chars")
        if "system_preview" in payload_info:
            lines.append(
                f"  System preview  : {payload_info['system_preview'][:150]!r}"
            )

        if "user_content_chars" in payload_info:
            lines.append(
                f"  User content    : {payload_info['user_content_chars']:,} chars (sanitized)"
            )

        if fmt == "gemini" and "contents_turns" in payload_info:
            lines.append(f"  Contents turns  : {payload_info['contents_turns']}")
        elif fmt == "openai" and "messages_count" in payload_info:
            lines.append(f"  Message count   : {payload_info['messages_count']}")

        if "api_model" in payload_info:
            lines.append(f"  API model field : {payload_info['api_model']}")
        if "api_params" in payload_info:
            lines.append(f"  API params      : {payload_info['api_params']}")
        if "generation_config" in payload_info:
            lines.append(f"  Gen config      : {payload_info['generation_config']}")
        if "response_mime_type" in payload_info:
            lines.append(f"  MIME type       : {payload_info['response_mime_type']}")

    lines.append("")
    lines.append("── CONTEXT ASSEMBLY ──")

    if payload_info.get("is_correction"):
        lines.append("  ⚠ CORRECTION REQUEST")
        if "correction_type" in payload_info:
            lines.append(f"  Correction type : {payload_info['correction_type']}")
        if "correction_preview" in payload_info:
            lines.append(f"  Correction msg  : {payload_info['correction_preview']}")
    else:
        keys = payload_info.get("context_keys")
        if keys:
            lines.append(f"  Context keys    : {keys}")
        for label, key in [
            ("history (chat)  ", "history_current_chat"),
            ("history (recent)", "history_recent"),
            ("memories        ", "memories"),
            ("thoughts        ", "thoughts"),
            ("participants    ", "participants"),
            ("tags            ", "tags"),
        ]:
            v = payload_info.get(key)
            if v is not None:
                lines.append(f"  {label}: {v}")

        es = payload_info.get("emotion_state")
        if es:
            lines.append(f"  Emotion state   : {es[:150]}")
        last_msg = payload_info.get("last_chat_msg")
        if last_msg:
            lines.append(f"  Last chat msg   : {last_msg!r}")
        mems = payload_info.get("memory_sample")
        if mems:
            lines.append("  Memory sample   :")
            for m in mems:
                lines.append(f"    - {m!r}")

        if (
            not keys
            and "context_keys" not in payload_info
            and not payload_info.get("is_correction")
        ):
            lines.append("  (context blob not parsed — payload may be fully sanitized)")
            lines.append(
                "  Tip: use cortex_read() to see the raw sanitized payload structure."
            )

    lines.append("")
    lines.append("── LLM RESPONSE ──")

    if sess.resp_error:
        lines.append(f"  ERROR: {sess.resp_error}")
    else:
        actions = _parse_response_actions(sess.resp_body_text)
        if actions:
            lines.append(f"  Actions ({len(actions)}):")
            for a in actions:
                lines.append(f"    - type={a['type']}  keys={a['keys']}")
        else:
            # Show a preview of the raw response
            preview = sess.resp_body_text[:400].replace("\n", " ")
            lines.append(f"  Response preview: {preview!r}")

    return "\n".join(lines)


@mcp.tool()
def cortex_search(
    query: str,
    limit: int = 10,
    search_in: str = "both",
    since_minutes: Optional[int] = None,
) -> str:
    """Find sessions whose payload or response body match a keyword or regex.

    Args:
        query:         Plain text or Python regex (case-insensitive).
        limit:         Max matching sessions to return (default 10, max 50).
        search_in:     Where to search: 'payload', 'response', or 'both' (default).
        since_minutes: Restrict to sessions from the last N minutes.
    """
    limit = min(max(1, limit), 50)

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return f"Invalid regex: {exc}"

    # Load a large window to search
    all_sessions = _load_sessions(limit=500)

    if since_minutes is not None:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=since_minutes)

        def _ts_dt(ts: str) -> Optional[datetime]:
            try:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                return None

        all_sessions = [
            s
            for s in all_sessions
            if (dt := _ts_dt(s.req_ts)) is not None and dt >= cutoff
        ]

    results: list[str] = []
    for sess in all_sessions:
        if len(results) >= limit:
            break

        check_payload = search_in in ("payload", "both")
        check_response = search_in in ("response", "both")

        hit_payload = check_payload and bool(pattern.search(sess.req_payload_text))
        hit_response = check_response and bool(pattern.search(sess.resp_body_text))

        if not hit_payload and not hit_response:
            continue

        elapsed = f"{sess.resp_elapsed_ms // 1000}s" if sess.resp_elapsed_ms else "?"
        where = []
        if hit_payload:
            where.append("payload")
        if hit_response:
            where.append("response")

        # Extract a snippet around the first match
        snippets: list[str] = []
        for text, label in [
            (sess.req_payload_text, "payload"),
            (sess.resp_body_text, "response"),
        ]:
            if label not in where:
                continue
            m = pattern.search(text)
            if m:
                start = max(0, m.start() - 60)
                end = min(len(text), m.end() + 100)
                snippet = text[start:end].replace("\n", " ")
                snippets.append(f"  [{label}] ...{snippet}...")

        results.append(
            f"Session {sess.index} [{sess.req_ts}] {sess.engine} {sess.model} ({elapsed})\n"
            + "\n".join(snippets)
        )

    if not results:
        return f"No sessions matched '{query}'."

    header = f"{len(results)} match(es) for '{query}':\n"
    return header + "\n\n".join(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
