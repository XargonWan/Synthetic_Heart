from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rift_vessel.schema import WorldState


@dataclass
class VRChatWorldState(WorldState):
    world_name: str | None = None
    instance_id: str | None = None
    player_count: int = 0
    fps: float | None = None
    ping_ms: int | None = None
    tracked_avatar_params: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        lines = ["[World: VRChat]"]
        if self.world_name:
            lines.append(f"  World: {self.world_name}")
        if self.player_count > 0:
            lines.append(f"  Players: {self.player_count}")
        if self.fps is not None:
            lines.append(f"  FPS: {self.fps:.0f}")
        if self.tracked_avatar_params:
            top = dict(list(self.tracked_avatar_params.items())[:5])
            param_str = ", ".join(f"{k}={v}" for k, v in top.items())
            lines.append(f"  Avatar params: {param_str}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "world_name": self.world_name,
                "instance_id": self.instance_id,
                "player_count": self.player_count,
                "fps": self.fps,
                "ping_ms": self.ping_ms,
                "tracked_avatar_params": dict(self.tracked_avatar_params),
            }
        )
        return base
