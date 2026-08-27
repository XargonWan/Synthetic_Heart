"""Shared, gzip-aware log archive utilities.

This module is the single source of truth for the on-disk log naming scheme,
file discovery, transparent (gzip-aware) reading, and retention/compression.

It is deliberately dependency-free (stdlib only) so it can be imported both by
the running application (``core.logging_utils`` / ``core.webui``) and by the
standalone ``mcp_servers/synth_logs.py`` MCP server, which may run outside the
container with only the repository checkout available.

Naming scheme
-------------
Active file (currently being written, == today)::

    synth.log

Daily rotated file (plain text; today / yesterday kept uncompressed)::

    synth.2026-07-29.log

Intra-day split when a single day exceeds the size / line safety cap::

    synth.2026-07-29.1.log
    synth.2026-07-29.2.log

Compressed (days older than yesterday)::

    synth.2026-07-28.log.gz
    synth.2026-07-28.1.log.gz

Retention
---------
* Today and yesterday: plain text.
* Older than yesterday and within the retention window: gzip-compressed.
* Older than the retention window (default 7 days): deleted.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via environment / config registry)
# ---------------------------------------------------------------------------

#: Number of days of logs to keep before deletion (plain + gzip combined).
DEFAULT_RETENTION_DAYS = 7

#: Safety cap: rotate the active file within the same day if it grows past this
#: many bytes, producing ``<stem>.<date>.<N>.log`` shards.
DEFAULT_MAX_BYTES = 50_000_000

#: Safety cap on line count for the active file within the same day.
DEFAULT_MAX_LINES = 0  # 0 == disabled (size cap is the primary safety net)

_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
}

# Standard log line format produced by logging_utils.py:
# [2026-04-12 23:45:18 +0900] [INFO] [file.py:42] message
_STD_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[^\]]*)\] \[(\w+)\] \[(.+?)\] (.*)$"
)

# Files whose lines carry large LLM payloads; callers may want to truncate.
LARGE_PAYLOAD_STEMS: frozenset[str] = frozenset({"cortex_api", "live_api"})

# ---------------------------------------------------------------------------
# Naming: dated-file matcher
# ---------------------------------------------------------------------------

# Matches: <stem>.<YYYY-MM-DD>[.<N>].log[.gz]
_DATED_RE = re.compile(
    r"^(?P<stem>.+?)\.(?P<date>\d{4}-\d{2}-\d{2})(?:\.(?P<shard>\d+))?\.log(?P<gz>\.gz)?$"
)

# Legacy timestamped rotation (pre-overhaul): <stem>.<YYYY-MM-DD_HH-MM-SS>[...].log
_LEGACY_TS_RE = re.compile(
    r"^(?P<stem>.+?)\.(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})[^.]*\.log(?P<gz>\.gz)?$"
)

# Legacy numbered rotation: <stem>.log.<N>[.gz]
_LEGACY_NUM_RE = re.compile(r"^(?P<stem>.+?)\.log\.(?P<num>\d+)(?P<gz>\.gz)?$")


@dataclass(frozen=True)
class LogFile:
    """A discovered log file (active or rotated)."""

    path: Path
    stem: str
    #: The calendar day the file belongs to, if it can be derived.
    day: date | None
    #: Intra-day shard index (0 for the primary daily file / active file).
    shard: int
    #: Whether the file is gzip-compressed.
    gzipped: bool
    #: True for the currently-written ``<stem>.log`` file.
    active: bool


def _log_dir() -> Path:
    return Path(os.getenv("LOG_DIR", str(Path.cwd() / "logs")))


def _today() -> date:
    """Return today's date in the configured timezone (falls back to local)."""
    return datetime.now().date()


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def dated_name(stem: str, day: date, shard: int = 0, gzipped: bool = False) -> str:
    """Build the on-disk name for a dated (rotated) log file."""
    shard_part = f".{shard}" if shard else ""
    gz_part = ".gz" if gzipped else ""
    return f"{stem}.{day.isoformat()}{shard_part}.log{gz_part}"


def classify_file(path: Path) -> LogFile | None:
    """Classify a path as a SyntH log file, or return ``None`` if it isn't one."""
    name = path.name
    # Active file: <stem>.log (no date, no shard, no gz)
    if name.endswith(".log") and not (
        _DATED_RE.match(name) or _LEGACY_TS_RE.match(name) or _LEGACY_NUM_RE.match(name)
    ):
        return LogFile(
            path=path, stem=path.stem, day=_today(), shard=0, gzipped=False, active=True
        )

    m = _DATED_RE.match(name)
    if m:
        try:
            day = date.fromisoformat(m.group("date"))
        except ValueError:
            day = None
        shard = int(m.group("shard")) if m.group("shard") else 0
        return LogFile(
            path=path,
            stem=m.group("stem"),
            day=day,
            shard=shard,
            gzipped=bool(m.group("gz")),
            active=False,
        )

    m = _LEGACY_TS_RE.match(name)
    if m:
        try:
            day = datetime.strptime(m.group("ts"), "%Y-%m-%d_%H-%M-%S").date()
        except ValueError:
            day = None
        return LogFile(
            path=path,
            stem=m.group("stem"),
            day=day,
            shard=0,
            gzipped=bool(m.group("gz")),
            active=False,
        )

    m = _LEGACY_NUM_RE.match(name)
    if m:
        return LogFile(
            path=path,
            stem=m.group("stem"),
            day=None,
            shard=int(m.group("num")),
            gzipped=bool(m.group("gz")),
            active=False,
        )

    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def all_log_files(log_dir: Path | None = None) -> list[LogFile]:
    """Return every discovered log file in *log_dir* (unsorted)."""
    directory = log_dir or _log_dir()
    if not directory.exists():
        return []
    result: list[LogFile] = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        lf = classify_file(p)
        if lf is not None:
            result.append(lf)
    return result


def active_stems(log_dir: Path | None = None) -> list[str]:
    """Return the logical names of all active (non-rotated) log files, sorted."""
    return sorted(lf.stem for lf in all_log_files(log_dir) if lf.active)


def all_stems(log_dir: Path | None = None) -> list[str]:
    """Return every stem seen across active and rotated files, sorted."""
    return sorted({lf.stem for lf in all_log_files(log_dir)})


def files_for_stem(
    stem: str, log_dir: Path | None = None, *, include_active: bool = True
) -> list[LogFile]:
    """Return files belonging to *stem*, newest-first (active file first)."""
    files = [lf for lf in all_log_files(log_dir) if lf.stem == stem]
    if not include_active:
        files = [lf for lf in files if not lf.active]

    def _key(lf: LogFile) -> tuple[int, float]:
        # Active file always first; then by mtime (newest first).
        try:
            mtime = lf.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (1 if lf.active else 0, mtime)

    files.sort(key=_key, reverse=True)
    return files


# ---------------------------------------------------------------------------
# Transparent (gzip-aware) reading
# ---------------------------------------------------------------------------


def open_text(path: Path):
    """Open a log file for text reading, transparently handling gzip."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def read_lines(path: Path) -> list[str]:
    """Read all lines from a (possibly gzipped) log file."""
    try:
        with open_text(path) as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def tail_lines(path: Path, n_lines: int) -> list[str]:
    """Return the last *n_lines* from *path* (gzip-aware).

    For plain files this reads only the tail; for gzip files the whole file is
    decompressed (gzip does not support efficient reverse seeking).
    """
    if n_lines <= 0:
        return []
    if path.suffix == ".gz":
        lines = read_lines(path)
        return lines[-n_lines:]
    chunk = 8 * 1024
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            buf = b""
            while pos > 0:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                lines = buf.decode("utf-8", errors="replace").splitlines()
                if len(lines) > n_lines:
                    return lines[-n_lines:]
            return buf.decode("utf-8", errors="replace").splitlines()[-n_lines:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Line parsing / search
# ---------------------------------------------------------------------------


def parse_line(line: str) -> tuple[datetime | None, str | None, str | None, str]:
    """Parse a standard log line into (timestamp, level, source, message).

    Returns ``(None, None, None, line)`` for non-standard (banner) lines.
    """
    m = _STD_LINE_RE.match(line)
    if not m:
        return None, None, None, line
    ts_str, level, source, message = m.groups()
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        ts = None
    return ts, level.upper(), source, message


@dataclass
class LogHit:
    """A single matching log line."""

    stem: str
    file: str
    level: str | None
    timestamp: str | None
    line: str


def search(
    *,
    query: str = "",
    is_regex: bool = False,
    stems: Iterable[str] | None = None,
    level: str | None = None,
    since: datetime | None = None,
    max_results: int = 500,
    log_dir: Path | None = None,
    truncate_large: int | None = None,
) -> list[LogHit]:
    """Search log files (including gzip) with text/regex/level/time filters.

    Parameters
    ----------
    query:
        Text substring (case-insensitive) or regex pattern. Empty matches all.
    is_regex:
        Treat *query* as a Python regex.
    stems:
        Restrict to these stems; ``None`` searches every stem.
    level:
        Minimum level (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
    since:
        Only lines with a parsed timestamp at/after this instant.
    max_results:
        Cap on returned hits.
    truncate_large:
        If set, truncate each matched line from large-payload stems to this
        many characters.
    """
    pattern: re.Pattern[str] | None = None
    if query:
        if is_regex:
            pattern = re.compile(query, re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

    min_level = _LEVEL_ORDER.get((level or "").upper()) if level else None

    directory = log_dir or _log_dir()
    target_stems = list(stems) if stems is not None else all_stems(directory)

    hits: list[LogHit] = []
    for stem in target_stems:
        for lf in files_for_stem(stem, directory):
            for raw in read_lines(lf.path):
                if pattern is not None and not pattern.search(raw):
                    continue
                ts, line_level, _src, _msg = parse_line(raw)
                if min_level is not None:
                    if line_level is None:
                        continue
                    if _LEVEL_ORDER.get(line_level, 0) < min_level:
                        continue
                if since is not None:
                    if ts is None or ts < since:
                        continue
                display = raw
                if truncate_large and stem in LARGE_PAYLOAD_STEMS:
                    display = raw[:truncate_large]
                hits.append(
                    LogHit(
                        stem=stem,
                        file=lf.path.name,
                        level=line_level,
                        timestamp=ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
                        line=display,
                    )
                )
                if len(hits) >= max_results:
                    return hits
    return hits


# ---------------------------------------------------------------------------
# Retention & compression
# ---------------------------------------------------------------------------


def _compress_file(path: Path) -> Path | None:
    """Gzip *path* in place, returning the new ``.gz`` path (or ``None``)."""
    if path.suffix == ".gz":
        return path
    gz_path = path.with_name(path.name + ".gz")
    try:
        with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        os.remove(path)
        return gz_path
    except OSError:
        # Clean up a partial gz on failure.
        try:
            if gz_path.exists():
                os.remove(gz_path)
        except OSError:
            pass
        return None


def enforce_retention(
    log_dir: Path | None = None,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    keep_plain_days: int = 2,
    today: date | None = None,
) -> dict[str, int]:
    """Compress older logs and delete logs beyond the retention window.

    * Files whose day is older than ``keep_plain_days`` (today + yesterday) and
      still plain text are gzip-compressed.
    * Files whose day is older than ``retention_days`` are deleted.

    Returns a summary ``{"compressed": n, "deleted": n}``. Best-effort — never
    raises; individual file errors are swallowed.
    """
    directory = log_dir or _log_dir()
    ref = today or _today()
    plain_cutoff = ref - timedelta(days=max(0, keep_plain_days - 1))
    delete_cutoff = ref - timedelta(days=max(1, retention_days))

    compressed = 0
    deleted = 0

    for lf in all_log_files(directory):
        if lf.active:
            continue  # never touch the live file
        if lf.day is None:
            continue  # can't age an undated legacy file safely
        # Deletion first.
        if lf.day < delete_cutoff:
            try:
                os.remove(lf.path)
                deleted += 1
            except OSError:
                pass
            continue
        # Compression for plain files older than the plain window.
        if not lf.gzipped and lf.day < plain_cutoff:
            if _compress_file(lf.path) is not None:
                compressed += 1

    return {"compressed": compressed, "deleted": deleted}


def iter_all_files(log_dir: Path | None = None) -> Iterator[Path]:
    """Yield every real file in the log directory (for archive download)."""
    directory = log_dir or _log_dir()
    if not directory.exists():
        return
    for p in sorted(directory.iterdir()):
        if p.is_file():
            yield p
