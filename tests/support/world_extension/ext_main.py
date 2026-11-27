"""A test world as a Vessel connector: the SDK's connector class against a real process."""

from __future__ import annotations

import asyncio
from typing import Any

from synth_sdk.runtime import Context
from synth_sdk.vessel import ActionResult, Emit, VesselConnector


class TinyWorld(VesselConnector):
    def __init__(self) -> None:
        super().__init__()
        self.connected = False
        self.emit: Emit | None = None
        self.room = "hall"
        self.busy = False

    async def connect(self, settings: dict[str, Any], emit: Emit) -> bool:
        if settings.get("host") == "down":
            self.last_error = "the server is down"
            return False
        self.emit, self.connected = emit, True
        await emit({"event_type": "chat", "summary": "welcome", "actor": "ana"})
        return True

    async def disconnect(self) -> None:
        self.connected = False

    async def act(self, action: str, payload: dict[str, Any]) -> ActionResult:
        if action == "move":
            self.room = str(payload.get("to") or self.room)
            self.changed()
            return ActionResult(True, f"now in {self.room}", {"room": self.room})
        if action == "wait":
            self.busy = True
            await asyncio.sleep(0.05)
            self.busy = False
            return ActionResult(True)
        return ActionResult(False, f"unknown action {action}")

    async def get_active_goal(self) -> dict[str, Any] | None:
        return {"text": "visit the kitchen"}

    @property
    def is_connected(self) -> bool:
        return self.connected

    def describe_surroundings(self) -> str | None:
        return f"You are in the {self.room}."

    def get_world_actions(self) -> dict[str, dict[str, Any]]:
        return {"move": {"required_fields": ["to"]}}

    def idle_busy(self) -> bool:
        return self.busy


async def main(ctx: Context) -> None:
    ctx.vessels.serve("tinyworld", TinyWorld())
    await ctx.stopping.wait()
