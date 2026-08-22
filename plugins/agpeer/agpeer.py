# plugins/agpeer/agpeer.py
"""agpeer plugin — P2P search and downloads for Synth.

**agpeer** is a homebrew agentic P2P client (Soulseek + torrent/magnet
backends) that exposes a loopback REST API (default ``http://127.0.0.1:41000``,
bearer-token auth). This plugin talks to that API directly — no MCP subprocess,
no agpeer code inside SyntH — and exposes a curated but complete action set so
Synth can find and pull files on request ("get me X song in flac, put it in
folder Y"), add magnets/torrents, manage transfers, inspect the organized
library, and read (or, deliberately gated, change) the core's runtime settings.

Design notes
------------
* **Agent Lane only, structurally.** Every action declares
  ``external_effects`` — search and status included — so any reply containing
  an agpeer call is classified to the Agent Lane by the router (multi-step
  search → pick → download → poll composition), and the Fast-Lane catalog
  stays free of P2P verbs. Purely structural routing, no keyword logic.
* **Destination sandbox.** Synth may only direct downloads inside
  ``AGPEER_DOWNLOAD_ROOT`` (read-write; agpeer's own writer already lives
  there). Relative folders resolve against the root; anything escaping it
  fails closed. agpeer's built-in default root is used when no destination
  is given.
* **Destructive verbs are double-gated.** ``agpeer_cancel_transfer`` /
  ``agpeer_delete_transfer`` additionally require
  ``AGPEER_ALLOW_DESTRUCTIVE``; without it they fail closed with a clear
  error the agent can relay.
* **Fail closed, never fatal.** The agpeer core not running, a wrong token,
  or a network blip returns a clean per-action error; importing or removing
  this plugin never breaks the rest of Synth.
* **Secret hygiene.** ``AGPEER_TOKEN`` is stored in Synth's user-owned config
  store, rendered masked in the WebUI, and never logged or echoed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.plugin_base import PluginBase

LOG_PREFIX = "[agpeer]"

_DEFAULT_API_BASE = "http://127.0.0.1:41000"
_DEFAULT_DOWNLOAD_ROOT = "E:\\Media\\Music"

# ---------------------------------------------------------------------------
# Configuration (plugin banner in the WebUI Plugins tab)
# ---------------------------------------------------------------------------

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "AGPEER_API_BASE",
        label="agpeer API Base URL",
        default=_DEFAULT_API_BASE,
        value_type=str,
        ui_type="string",
        description=(
            "Base URL of the local agpeer core's REST API (the same /api/v1 "
            "the agpeer desktop UI uses)."
        ),
        scope="agpeer",
        component="agpeer",
        advanced=True,
    )
    register_exposed_var(
        "AGPEER_TOKEN",
        label="agpeer API Token",
        default="",
        value_type=str,
        ui_type="password",
        description=(
            "Bearer token for the agpeer core API. Find it in the agpeer "
            "core's data directory as the 'token' file (<data_dir>/token). "
            "Stored masked; never logged."
        ),
        scope="agpeer",
        component="agpeer",
    )
    register_exposed_var(
        "AGPEER_DOWNLOAD_ROOT",
        label="agpeer Download Root",
        default=_DEFAULT_DOWNLOAD_ROOT,
        value_type=str,
        ui_type="string",
        description=(
            "The only directory tree agpeer downloads may be directed to "
            "(read-write sandbox). Relative 'destination' folders in actions "
            "resolve inside this root; anything escaping it is rejected."
        ),
        scope="agpeer",
        component="agpeer",
    )
    register_exposed_var(
        "AGPEER_TIMEOUT_SEC",
        label="agpeer Request Timeout (s)",
        default=15,
        value_type=int,
        ui_type="number",
        description="Per-request timeout for calls to the agpeer core.",
        scope="agpeer",
        component="agpeer",
        advanced=True,
    )
    register_exposed_var(
        "AGPEER_ALLOW_DESTRUCTIVE",
        label="agpeer Allow Cancel/Delete",
        default=False,
        value_type=bool,
        ui_type="bool",
        description=(
            "When off (default), agpeer_cancel_transfer and "
            "agpeer_delete_transfer fail closed — Synth cannot cancel or "
            "delete transfers (or their downloaded data)."
        ),
        scope="agpeer",
        component="agpeer",
        dangerous=True,
        advanced=True,
    )
except Exception:  # pragma: no cover - registration is best-effort
    pass


def _cfg_str(key: str, default: str) -> str:
    try:
        val = config_registry.get_value(key, default)
        return str(val).strip() if val is not None else default
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config_registry.get_value(key, default))
    except Exception:
        return default


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        val = config_registry.get_value(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Destination sandbox
# ---------------------------------------------------------------------------

def resolve_agpeer_destination(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """Resolve a requested download destination inside the sandbox root.

    Returns ``(absolute_path_or_None, error_or_None)``:

    * empty/missing → ``(None, None)`` — no destination is sent and agpeer's
      own configured default root is used;
    * relative folder → resolved against ``AGPEER_DOWNLOAD_ROOT``;
    * absolute path → must already be inside the root.

    Traversal or any path that escapes the root fails closed with an error.
    Comparison is case-insensitive so Windows drive-letter casing never
    produces a false rejection.
    """
    text = str(raw or "").strip()
    if not text:
        return None, None
    root_raw = _cfg_str("AGPEER_DOWNLOAD_ROOT", _DEFAULT_DOWNLOAD_ROOT)
    root = Path(root_raw).expanduser().resolve()
    candidate = Path(text).expanduser()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        inside = resolved.is_relative_to(root)
    except Exception:  # pragma: no cover - very old Python guard
        inside = str(resolved).lower().startswith(str(root).lower())
    if not inside:
        return (
            None,
            (
                f"destination '{text}' is outside the agpeer download sandbox "
                f"'{root}'; only paths inside it are allowed"
            ),
        )
    return str(resolved), None


# ---------------------------------------------------------------------------
# REST client (thin, fail-closed)
# ---------------------------------------------------------------------------

_MAX_FILE_SELECTION = 500


def _coerce_file_selection(raw: Any) -> list[Dict[str, Any]]:
    """Normalise a torrent ``file_selection`` payload for ``add_transfer``.

    Accepts a list of ``{"index": "0", "selected": true}`` entries (indices
    are strings per the agpeer API); boolean-ish strings ("false"/"0"/"off")
    coerce to False like the config helpers; invalid entries are dropped and
    the list is capped. Purely structural — never inspects file names.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or "index" not in item:
            continue
        selected = item.get("selected")
        if isinstance(selected, str):
            selected = selected.strip().lower() not in ("false", "0", "no", "off", "")
        else:
            selected = bool(selected)
        out.append({"index": str(item["index"]), "selected": selected})
        if len(out) >= _MAX_FILE_SELECTION:
            break
    return out


def _clip(text: str, limit: int = 300) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def agpeer_request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """One authenticated call to the agpeer core's ``/api/v1``.

    ``json_body`` may be any JSON value (dict for most endpoints, or a raw
    value for ``PUT /settings/{key}``). Fail-closed dict result:
    ``{"ok": True, "data": <parsed JSON or None>}`` or
    ``{"ok": False, "error": "<operator-readable message>"}``. Never raises,
    never logs the bearer token.
    """
    base = _cfg_str("AGPEER_API_BASE", _DEFAULT_API_BASE).rstrip("/")
    token = _cfg_str("AGPEER_TOKEN", "")
    timeout_s = max(1, min(_cfg_int("AGPEER_TIMEOUT_SEC", 15), 120))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base}{path}"
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            async with session.request(
                method, url, json=json_body, params=params, headers=headers
            ) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    return {
                        "ok": False,
                        "error": (
                            f"agpeer responded with {resp.status}: "
                            f"{_clip(body_text)}"
                        ),
                    }
                if not body_text.strip():
                    return {"ok": True, "data": None}
                try:
                    return {"ok": True, "data": json.loads(body_text)}
                except (TypeError, ValueError):
                    return {"ok": True, "data": _clip(body_text)}
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"agpeer request timed out after {timeout_s}s ({url})",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"agpeer core unreachable at {base} — is it running? "
                f"({type(exc).__name__}: {_clip(exc, 160)})"
            ),
        }


def _result_to_action(
    outcome: Dict[str, Any], pick: list[str] | None = None
) -> Dict[str, Any]:
    """Convert an ``agpeer_request`` outcome into a plugin action result.

    ``pick`` names top-level keys to hoist out of the response JSON (e.g.
    ``search_id``) so the model sees them without digging through ``data``;
    the full response is always kept under ``data`` as well.
    """
    if not outcome.get("ok"):
        return {
            "status": "error",
            "message": str(outcome.get("error") or "unknown agpeer error"),
        }
    data = outcome.get("data")
    result: Dict[str, Any] = {"status": "ok", "data": data}
    if pick and isinstance(data, dict):
        for key in pick:
            if key in data:
                result[key] = data[key]
    return result


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class AgpeerPlugin(PluginBase):
    """agpeer P2P client plugin (search, download, transfer lifecycle).

    Every action declares ``external_effects`` so agpeer calls ride the Agent
    Lane (structural routing — the Fast Lane never sees a P2P verb). The
    plugin is fully optional: absent core, wrong token, or a disabled plugin
    degrade to clean per-action errors.
    """

    display_name = "agpeer"

    def __init__(self) -> None:
        super().__init__()
        try:
            from core.core_initializer import register_plugin

            register_plugin("agpeer", self)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} register_plugin failed: {exc}")
        log_info(f"{LOG_PREFIX} AgpeerPlugin registered")

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "agpeer",
            "display_name": "agpeer",
            "description": (
                "P2P search and downloads through the local agpeer core "
                "(Soulseek + torrent/magnet backends). Find files, download "
                "them into the configured media root, add magnets, manage "
                "transfers, and inspect the organized library. All agpeer "
                "actions run on the agent route; download destinations are "
                "sandboxed to the configured root."
            ),
            "category": "Various",
            "icon": "icon.svg",
            "guide": "guide.md",
            "disable_allowed": True,
        }

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
            "agpeer_status": {
                "description": (
                    "Check the agpeer P2P core: version, uptime, and each "
                    "backend's readiness (search_available / "
                    "transfer_available). Call this first if any other agpeer "
                    "action errors — it tells you whether the core is running "
                    "and which backends are usable."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_search": {
                "description": (
                    "Start a P2P file search. 'backend' selects the engine: "
                    "'soulseek' (peer-to-peer files — filter with "
                    "'extension' e.g. 'flac', 'user', 'min_size' bytes) or "
                    "'hook' (magnet/torrent search — each result carries "
                    "backend_metadata.magnet, a ready-to-use magnet URI, and "
                    "attributes.seeders/leechers; prefer more seeders). Put "
                    "what you're looking for in 'query' as free text (e.g. "
                    "'artist album flac'). The search runs asynchronously: "
                    "results accumulate over ~10-15 seconds, so wait a "
                    "little, then fetch them with agpeer_search_results "
                    "using the returned search_id. A 503 BackendUnavailable "
                    "for hook search means it is disabled in the core's "
                    "settings (hook_search.enabled)."
                ),
                "required_fields": ["query"],
                "optional_fields": [
                    "backend",
                    "extension",
                    "user",
                    "min_size",
                    "max_results",
                ],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_search_results": {
                "description": (
                    "Fetch the results accumulated so far for a search "
                    "(pass the search_id from agpeer_search). Each result "
                    "carries result_id, username (peer), filename, size, "
                    "bitrate, queue_length and free_upload_slots — prefer "
                    "peers with free_upload_slots=true and a low "
                    "queue_length, then hand the chosen result_id to "
                    "agpeer_download."
                ),
                "required_fields": ["search_id"],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_download": {
                "description": (
                    "Download one search result over P2P. Pass the "
                    "'search_id' and the chosen 'result_id' from "
                    "agpeer_search_results. Optionally a 'destination' "
                    "folder — a bare folder name is placed inside the "
                    "configured download root; paths outside it are "
                    "rejected. Omit 'destination' to use the default root. "
                    "Returns a transfer_id: poll its state with "
                    "agpeer_transfer until 'completed' (a busy peer may stay "
                    "'queued' for a while; if a peer refuses or stays "
                    "silent, pick a different result and download again)."
                ),
                "required_fields": ["search_id", "result_id"],
                "optional_fields": ["destination"],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
            "agpeer_add_magnet": {
                "description": (
                    "Add a torrent transfer directly. 'source' is a magnet "
                    "URI, a local .torrent file path, or an http(s):// URL "
                    "to a .torrent. Optionally a 'destination' folder (same "
                    "sandbox rules as agpeer_download) and a 'display_name'. "
                    "For multi-file torrents, optionally pass "
                    "'file_selection' as a list like "
                    "[{\"index\":\"0\",\"selected\":true}] to pick which "
                    "files download (indices are strings from "
                    "agpeer_transfer_files). A new transfer may sit in "
                    "'resolving' while it fetches magnet metadata from the "
                    "swarm — keep polling; several minutes there usually "
                    "means a dead swarm, so report and suggest another "
                    "source instead of waiting forever. Returns a "
                    "transfer_id to poll with agpeer_transfer."
                ),
                "required_fields": ["source"],
                "optional_fields": [
                    "destination",
                    "display_name",
                    "file_selection",
                ],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
            "agpeer_searches": {
                "description": (
                    "List past and active searches with their ids, states "
                    "and result counts — use it to recover a search_id you "
                    "already started (e.g. after a long pause) instead of "
                    "searching again."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_stop_search": {
                "description": (
                    "Stop collecting results for a search early by 'id' "
                    "(frees the core's search slot; already-collected "
                    "results remain fetchable for a while)."
                ),
                "required_fields": ["id"],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_transfer_files": {
                "description": (
                    "List the files of a multi-file torrent transfer by "
                    "'id', with per-file selection state and index — use "
                    "the index strings in agpeer_add_magnet's "
                    "'file_selection' when the user wants only a subset."
                ),
                "required_fields": ["id"],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_pause_transfer": {
                "description": (
                    "Pause a torrent transfer by 'id' (torrent backend "
                    "only; soulseek transfers reject pause/resume). Resume "
                    "later with agpeer_resume_transfer."
                ),
                "required_fields": ["id"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["network"],
            },
            "agpeer_resume_transfer": {
                "description": (
                    "Resume a paused torrent transfer by 'id' (torrent "
                    "backend only)."
                ),
                "required_fields": ["id"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["network"],
            },
            "agpeer_library": {
                "description": (
                    "List the organized media library (files under the "
                    "core's configured library root, directories first). "
                    "Use it to confirm a completed download was organized, "
                    "or to check what's already there before searching. "
                    "Empty when no library root is configured."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_postprocess": {
                "description": (
                    "Inspect post-processing jobs (auto-organize runs the "
                    "core performs on completed transfers when enabled). "
                    "With 'id': one job and its per-step states; without: "
                    "all jobs. A transfer's postprocess_state also shows "
                    "up on agpeer_transfer."
                ),
                "required_fields": [],
                "optional_fields": ["id"],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_settings": {
                "description": (
                    "Read the agpeer core's runtime settings (secrets are "
                    "redacted by the core). Without 'key': the full map; "
                    "with 'key': one setting (e.g. 'hook_search.enabled')."
                ),
                "required_fields": [],
                "optional_fields": ["key"],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_setting_set": {
                "description": (
                    "Set one agpeer core runtime setting ('key' e.g. "
                    "'hook_search.enabled', 'value' any JSON value) and "
                    "return the updated map. Only do this when the user "
                    "explicitly asks for a settings change — this alters "
                    "how the P2P client behaves. Secrets are never "
                    "settable here."
                ),
                "required_fields": ["key", "value"],
                "optional_fields": [],
                "security_level": "high",
                "external_effects": ["network"],
            },
            "agpeer_setting_delete": {
                "description": (
                    "Remove one agpeer core runtime setting override by "
                    "'key', restoring its default. Only do this when the "
                    "user explicitly asks for a settings change."
                ),
                "required_fields": ["key"],
                "optional_fields": [],
                "security_level": "high",
                "external_effects": ["network"],
            },
            "agpeer_transfer": {
                "description": (
                    "Check transfer progress. With 'id': one transfer's "
                    "state (queued|resolving|downloading|paused|completed|"
                    "failed|cancelled), progress (0..1), bytes, "
                    "destination and error. Without 'id': the full "
                    "transfer list. Poll a download every ~3-4 seconds "
                    "until its state is terminal; 'resolving' means a "
                    "magnet is still fetching metadata from the swarm."
                ),
                "required_fields": [],
                "optional_fields": ["id"],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "agpeer_cancel_transfer": {
                "description": (
                    "Stop a running transfer by 'id'. The partial file is "
                    "kept unless the operator has enabled destructive "
                    "operations — when they haven't, this action is refused "
                    "by configuration and you should say so instead."
                ),
                "required_fields": ["id"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["network"],
            },
            "agpeer_delete_transfer": {
                "description": (
                    "Remove a transfer from agpeer's list by 'id'. With "
                    "'delete_data' true the downloaded/partial files are "
                    "deleted too. Destructive: refused unless the operator "
                    "has enabled destructive operations — when they haven't, "
                    "this action is refused by configuration and you should "
                    "say so instead."
                ),
                "required_fields": ["id"],
                "optional_fields": ["delete_data"],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
        }

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Dict[str, Any] | None = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        """Dispatch the ``agpeer_*`` actions (all fail-safe)."""
        action = action or {}
        action_name = str(action.get("type") or "")
        payload = action.get("payload") or {}
        try:
            if action_name == "agpeer_status":
                outcome = await agpeer_request("GET", "/api/v1/status")
                return _result_to_action(outcome)

            if action_name == "agpeer_search":
                query = str(payload.get("query") or "").strip()
                if not query:
                    return {
                        "status": "error",
                        "message": "agpeer_search requires a free-text 'query'",
                    }
                body: Dict[str, Any] = {
                    "backend": str(payload.get("backend") or "soulseek").strip()
                    or "soulseek",
                    "query": query,
                }
                for key in ("extension", "user"):
                    val = str(payload.get(key) or "").strip()
                    if val:
                        body[key] = val
                if payload.get("min_size") is not None:
                    try:
                        body["min_size"] = int(payload.get("min_size"))
                    except (TypeError, ValueError):
                        pass
                if payload.get("max_results") is not None:
                    try:
                        body["max_results"] = max(
                            1, min(int(payload.get("max_results")), 200)
                        )
                    except (TypeError, ValueError):
                        pass
                outcome = await agpeer_request("POST", "/api/v1/searches", json_body=body)
                result = _result_to_action(outcome, pick=["search_id"])
                if result.get("status") == "ok":
                    result["note"] = (
                        "search started; results accumulate over ~10-15s — "
                        "fetch them with agpeer_search_results"
                    )
                return result

            if action_name == "agpeer_search_results":
                search_id = str(payload.get("search_id") or "").strip()
                if not search_id:
                    return {
                        "status": "error",
                        "message": "agpeer_search_results requires 'search_id'",
                    }
                outcome = await agpeer_request(
                    "GET", f"/api/v1/searches/{quote(search_id, safe='')}/results"
                )
                if not outcome.get("ok"):
                    return {
                        "status": "error",
                        "message": str(outcome.get("error") or "agpeer error"),
                    }
                data = outcome.get("data")
                results = data if isinstance(data, list) else (data or {}).get("results", [])
                return {
                    "status": "ok",
                    "results": results,
                    "count": len(results) if isinstance(results, list) else 0,
                }

            if action_name == "agpeer_download":
                search_id = str(payload.get("search_id") or "").strip()
                result_id = str(payload.get("result_id") or "").strip()
                if not search_id or not result_id:
                    return {
                        "status": "error",
                        "message": (
                            "agpeer_download requires 'search_id' and 'result_id'"
                        ),
                    }
                destination, dest_error = resolve_agpeer_destination(
                    payload.get("destination")
                )
                if dest_error:
                    return {"status": "error", "message": dest_error}
                body = {"destination": destination} if destination else {}
                outcome = await agpeer_request(
                    "POST",
                    (
                        f"/api/v1/searches/{quote(search_id, safe='')}"
                        f"/results/{quote(result_id, safe='')}/download"
                    ),
                    json_body=body,
                )
                result = _result_to_action(outcome, pick=["transfer_id"])
                if result.get("status") == "ok":
                    result["note"] = (
                        "download started; poll agpeer_transfer with this "
                        "transfer_id until state is 'completed'"
                    )
                return result

            if action_name == "agpeer_add_magnet":
                source = str(payload.get("source") or "").strip()
                if not source:
                    return {
                        "status": "error",
                        "message": (
                            "agpeer_add_magnet requires a 'source' (magnet "
                            "URI, .torrent path, or .torrent URL)"
                        ),
                    }
                destination, dest_error = resolve_agpeer_destination(
                    payload.get("destination")
                )
                if dest_error:
                    return {"status": "error", "message": dest_error}
                body: Dict[str, Any] = {"backend": "torrent", "source": source}
                if destination:
                    body["destination"] = destination
                display_name = str(payload.get("display_name") or "").strip()
                if display_name:
                    body["display_name"] = display_name
                file_selection = _coerce_file_selection(payload.get("file_selection"))
                if file_selection:
                    body["file_selection"] = file_selection
                outcome = await agpeer_request(
                    "POST", "/api/v1/transfers", json_body=body
                )
                return _result_to_action(outcome, pick=["transfer_id"])

            if action_name == "agpeer_searches":
                outcome = await agpeer_request("GET", "/api/v1/searches")
                return _result_to_action(outcome)

            if action_name == "agpeer_stop_search":
                search_id = str(payload.get("id") or "").strip()
                if not search_id:
                    return {
                        "status": "error",
                        "message": "agpeer_stop_search requires 'id'",
                    }
                outcome = await agpeer_request(
                    "POST", f"/api/v1/searches/{quote(search_id, safe='')}/stop"
                )
                return _result_to_action(outcome)

            if action_name == "agpeer_transfer_files":
                transfer_id = str(payload.get("id") or "").strip()
                if not transfer_id:
                    return {
                        "status": "error",
                        "message": "agpeer_transfer_files requires 'id'",
                    }
                outcome = await agpeer_request(
                    "GET", f"/api/v1/transfers/{quote(transfer_id, safe='')}/files"
                )
                return _result_to_action(outcome)

            if action_name in ("agpeer_pause_transfer", "agpeer_resume_transfer"):
                transfer_id = str(payload.get("id") or "").strip()
                if not transfer_id:
                    return {
                        "status": "error",
                        "message": f"{action_name} requires 'id'",
                    }
                verb = "pause" if action_name == "agpeer_pause_transfer" else "resume"
                outcome = await agpeer_request(
                    "POST",
                    f"/api/v1/transfers/{quote(transfer_id, safe='')}/{verb}",
                )
                return _result_to_action(outcome)

            if action_name == "agpeer_library":
                outcome = await agpeer_request("GET", "/api/v1/library")
                return _result_to_action(outcome)

            if action_name == "agpeer_postprocess":
                job_id = str(payload.get("id") or "").strip()
                if job_id:
                    outcome = await agpeer_request(
                        "GET", f"/api/v1/postprocess/{quote(job_id, safe='')}"
                    )
                else:
                    outcome = await agpeer_request("GET", "/api/v1/postprocess")
                return _result_to_action(outcome)

            if action_name == "agpeer_settings":
                key = str(payload.get("key") or "").strip()
                if key:
                    outcome = await agpeer_request(
                        "GET", f"/api/v1/settings/{quote(key, safe='')}"
                    )
                else:
                    outcome = await agpeer_request("GET", "/api/v1/settings")
                return _result_to_action(outcome)

            if action_name in ("agpeer_setting_set", "agpeer_setting_delete"):
                key = str(payload.get("key") or "").strip()
                if not key:
                    return {
                        "status": "error",
                        "message": f"{action_name} requires 'key'",
                    }
                # Dotted keys are legal (hook_search.enabled); anything that
                # could smuggle extra path segments is rejected.
                if "/" in key or "\\" in key or any(ch.isspace() for ch in key):
                    return {
                        "status": "error",
                        "message": f"invalid setting key {key!r}",
                    }
                if action_name == "agpeer_setting_set":
                    if "value" not in payload:
                        return {
                            "status": "error",
                            "message": "agpeer_setting_set requires 'value'",
                        }
                    outcome = await agpeer_request(
                        "PUT",
                        f"/api/v1/settings/{quote(key, safe='')}",
                        json_body=payload.get("value"),
                    )
                else:
                    outcome = await agpeer_request(
                        "DELETE", f"/api/v1/settings/{quote(key, safe='')}"
                    )
                return _result_to_action(outcome)

            if action_name == "agpeer_transfer":
                transfer_id = str(payload.get("id") or "").strip()
                if transfer_id:
                    outcome = await agpeer_request(
                        "GET", f"/api/v1/transfers/{quote(transfer_id, safe='')}"
                    )
                else:
                    outcome = await agpeer_request("GET", "/api/v1/transfers")
                return _result_to_action(outcome)

            if action_name in (
                "agpeer_cancel_transfer",
                "agpeer_delete_transfer",
            ):
                transfer_id = str(payload.get("id") or "").strip()
                if not transfer_id:
                    return {
                        "status": "error",
                        "message": f"{action_name} requires 'id'",
                    }
                if not _cfg_bool("AGPEER_ALLOW_DESTRUCTIVE", False):
                    return {
                        "status": "error",
                        "message": (
                            f"{action_name} is disabled by configuration "
                            "(AGPEER_ALLOW_DESTRUCTIVE is off) — ask the "
                            "operator to enable it if this is intended"
                        ),
                    }
                if action_name == "agpeer_cancel_transfer":
                    outcome = await agpeer_request(
                        "POST",
                        f"/api/v1/transfers/{quote(transfer_id, safe='')}/cancel",
                    )
                    return _result_to_action(outcome)
                params: Dict[str, Any] = {}
                if bool(payload.get("delete_data")):
                    params["delete_data"] = "true"
                outcome = await agpeer_request(
                    "DELETE",
                    f"/api/v1/transfers/{quote(transfer_id, safe='')}",
                    params=params,
                )
                return _result_to_action(outcome)

            return {"status": "error", "message": f"unknown_action:{action_name}"}
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} action '{action_name}' failed: {exc}")
            return {"status": "error", "message": str(exc)}


PLUGIN_CLASS = AgpeerPlugin
