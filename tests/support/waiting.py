"""Waiting for something that happens in another task or process."""

from __future__ import annotations

import asyncio
from typing import Any


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    """Poll ``predicate`` until it is true; raises ``TimeoutError`` after ``timeout`` seconds."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)
