"""Process entry of an extension. Run by ``synth_sdk/_launch.py``, not by authors.

Sets the process up the way the Core expects, then hands control to
:func:`synth_sdk.runtime.run_extension`:

* applies the resource limits the Core asked for (``SYNTH_EXT_LIMITS``);
* moves the protocol onto private file descriptors and points ``stdout`` at
  ``stderr``, so a stray ``print`` cannot corrupt the protocol;
* gives the extension an empty ``stdin``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


def _apply_limits(raw: str) -> None:
    """Lower resource limits (soft and hard: the extension cannot raise them)."""
    try:
        import resource
    except ImportError:  # not POSIX
        return
    try:
        limits = json.loads(raw) if raw else {}
    except ValueError:
        return
    names = {
        "nofile": resource.RLIMIT_NOFILE,
        "nproc": getattr(resource, "RLIMIT_NPROC", None),
        "fsize": resource.RLIMIT_FSIZE,
        "core": resource.RLIMIT_CORE,
        "as": resource.RLIMIT_AS,
        "cpu": resource.RLIMIT_CPU,
    }
    for name, value in limits.items():
        which = names.get(name)
        if which is None or not isinstance(value, int) or value < 0:
            continue
        try:
            _soft, hard = resource.getrlimit(which)
            target = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (target, target))
        except (ValueError, OSError):
            continue


def main() -> int:
    extension_id = os.environ.get("SYNTH_EXTENSION_ID", "")
    entry = os.environ.get("SYNTH_EXTENSION_ENTRY", "")
    data_dir = Path(os.environ.get("SYNTH_EXTENSION_DATA", "."))
    if not extension_id or not entry:
        sys.stderr.write(
            "synth_sdk: SYNTH_EXTENSION_ID and SYNTH_EXTENSION_ENTRY are required\n"
        )
        return 64
    _apply_limits(os.environ.get("SYNTH_EXT_LIMITS", ""))

    # Take private copies of the protocol pipes, then neutralise fds 0 and 1.
    in_fd = os.dup(0)
    out_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)

    from synth_sdk import runtime

    async def go() -> int:
        reader, writer = await runtime.open_streams(in_fd, out_fd)
        return await runtime.run_extension(
            reader,
            writer,
            extension_id=extension_id,
            data_dir=data_dir,
            entry=entry,
            stop_grace=float(os.environ.get("SYNTH_EXT_STOP_GRACE", "5")),
        )

    return asyncio.run(go())
