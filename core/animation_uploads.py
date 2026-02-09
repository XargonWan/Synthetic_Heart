"""Helpers for managing temporary animation uploads.

Temporary uploads live under skins/temp/<upload_id>/animations/<state>/.
Metadata is stored per-upload in skins/temp/<upload_id>/meta.json.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_info, log_warning

UPLOADS_ROOT = Path("skins/temp")
META_FILENAME = "meta.json"

ALLOWED_ANIMATION_STATES = {"idle", "think", "write", "talk"}


def ensure_uploads_root() -> Path:
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    return UPLOADS_ROOT


def normalize_state(state: str) -> str:
    if not state:
        raise ValueError("state is required")
    normalized = str(state).strip().lower()
    if normalized not in ALLOWED_ANIMATION_STATES:
        raise ValueError(f"unsupported animation state: {state}")
    return normalized


def sanitize_upload_id(upload_id: Optional[str]) -> str:
    if upload_id:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", str(upload_id))
        if cleaned:
            return cleaned
    return uuid.uuid4().hex


def sanitize_filename(filename: str) -> str:
    return Path(filename).name


def get_upload_root(upload_id: str) -> Path:
    return ensure_uploads_root() / upload_id


def get_state_dir(upload_id: str, state: str) -> Path:
    root = get_upload_root(upload_id)
    return root / "animations" / normalize_state(state)


def _meta_path(upload_id: str) -> Path:
    return get_upload_root(upload_id) / META_FILENAME


def read_meta(upload_id: str) -> Optional[Dict[str, Any]]:
    path = _meta_path(upload_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        log_warning(f"[animation_uploads] Failed to read meta for {upload_id}: {exc}")
        return None


def write_meta(upload_id: str, meta: Dict[str, Any]) -> None:
    path = _meta_path(upload_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, sort_keys=True)
    except Exception as exc:
        log_warning(f"[animation_uploads] Failed to write meta for {upload_id}: {exc}")


def record_upload(
    upload_id: str,
    state: str,
    filename: str,
    *,
    size_bytes: int,
    tags: Optional[List[str]] = None,
    descriptor_path: Optional[Path] = None,
    original_filename: Optional[str] = None,
) -> Dict[str, Any]:
    meta = read_meta(upload_id) or {
        "upload_id": upload_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "states": {},
        "tags": tags or [],
    }

    normalized_state = normalize_state(state)
    meta.setdefault("states", {})
    state_entries = meta["states"].setdefault(normalized_state, [])

    entry = {
        "name": filename,
        "size_bytes": size_bytes,
        "descriptor": str(descriptor_path) if descriptor_path else None,
        "original_filename": original_filename or filename,
        "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    state_entries.append(entry)

    if tags:
        meta["tags"] = tags

    write_meta(upload_id, meta)
    return meta


def list_uploads() -> List[Dict[str, Any]]:
    ensure_uploads_root()
    uploads: List[Dict[str, Any]] = []
    for entry in UPLOADS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        meta = read_meta(entry.name)
        if not meta:
            continue
        uploads.append(meta)
    return uploads


def delete_upload(upload_id: str) -> bool:
    root = get_upload_root(upload_id)
    if not root.exists():
        return False
    try:
        shutil.rmtree(root)
        log_info(f"[animation_uploads] Deleted upload {upload_id}")
        return True
    except Exception as exc:
        log_warning(f"[animation_uploads] Failed to delete upload {upload_id}: {exc}")
        return False


def promote_upload(
    upload_id: str,
    *,
    target_skin: str,
    target_state: Optional[str] = None,
    overwrite: bool = False,
    rename: Optional[str] = None,
) -> List[Path]:
    meta = read_meta(upload_id)
    if not meta:
        raise FileNotFoundError(f"Upload {upload_id} not found")

    promoted: List[Path] = []
    states = meta.get("states", {})
    for state, files in states.items():
        source_state = state
        destination_state = normalize_state(target_state or source_state)
        for file_entry in files:
            fname = file_entry.get("name")
            if not fname:
                continue
            source_dir = get_state_dir(upload_id, source_state)
            source_path = source_dir / fname
            if not source_path.exists():
                continue

            dest_dir = Path("skins") / target_skin / "animations" / destination_state
            dest_dir.mkdir(parents=True, exist_ok=True)

            dest_name = rename or fname
            dest_path = dest_dir / dest_name
            if dest_path.exists() and not overwrite:
                raise FileExistsError(f"File already exists: {dest_path}")

            shutil.copy2(source_path, dest_path)
            promoted.append(dest_path)

            # Copy descriptor if present
            descriptor = source_path.with_suffix(source_path.suffix + ".json")
            if descriptor.exists():
                shutil.copy2(descriptor, dest_path.with_suffix(dest_path.suffix + ".json"))

    return promoted


def cleanup_expired_uploads(ttl_days: int) -> List[str]:
    """Remove uploads older than ttl_days.

    Returns a list of removed upload_ids.
    """
    removed: List[str] = []
    if ttl_days <= 0:
        return removed

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ttl_days)
    for entry in UPLOADS_ROOT.iterdir() if UPLOADS_ROOT.exists() else []:
        if not entry.is_dir():
            continue
        upload_id = entry.name
        meta = read_meta(upload_id)
        if not meta:
            continue
        created_at = meta.get("created_at")
        if not created_at:
            continue
        try:
            created_dt = datetime.fromisoformat(created_at)
        except Exception:
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        if created_dt <= cutoff:
            if delete_upload(upload_id):
                removed.append(upload_id)
    if removed:
        log_info(f"[animation_uploads] Removed expired uploads: {removed}")
    return removed
