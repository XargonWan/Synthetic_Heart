from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rift_vessel.schema import WorldState, EntityRef, ActionDef


@dataclass
class SkyrimWorldState(WorldState):
    location: str | None = None
    health: float | None = None
    max_health: float | None = None
    magicka: float | None = None
    stamina: float | None = None
    position: tuple[float, float, float] | None = None
    combat_state: bool = False
    is_sneaking: bool = False
    is_mounted: bool = False
    current_weapon: str | None = None
    current_spell: str | None = None
    current_shout: str | None = None
    level: int = 1
    carry_weight_pct: float | None = None
    gold: int = 0
    visible_entities: list[EntityRef] = field(default_factory=list)
    recent_dialogue: list[str] = field(default_factory=list)
    possible_actions: list[ActionDef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        lines = ["[World: Skyrim]"]
        if self.location:
            lines.append(f"  Location: {self.location}")
        if self.health is not None:
            hp = f"{self.health:.0f}"
            if self.max_health:
                hp += f"/{self.max_health:.0f}"
            lines.append(f"  Health: {hp}")
        if self.magicka is not None:
            lines.append(f"  Magicka: {self.magicka:.0f}")
        if self.stamina is not None:
            lines.append(f"  Stamina: {self.stamina:.0f}")
        if self.combat_state:
            lines.append("  \u2694 In combat")
        if self.is_sneaking:
            lines.append("  \ud83e\udd77 Sneaking")
        if self.level and self.level > 1:
            lines.append(f"  Level: {self.level}")
        if self.visible_entities:
            names = ", ".join(e.name for e in self.visible_entities[:5])
            lines.append(f"  Nearby: {names}")
        if self.possible_actions:
            names = ", ".join(a.name for a in self.possible_actions[:8])
            lines.append(f"  Can do: {names}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "health": self.health,
                "max_health": self.max_health,
                "magicka": self.magicka,
                "stamina": self.stamina,
                "location": self.location,
                "position": list(self.position) if self.position else None,
                "combat_state": self.combat_state,
                "is_sneaking": self.is_sneaking,
                "is_mounted": self.is_mounted,
                "current_weapon": self.current_weapon,
                "current_spell": self.current_spell,
                "current_shout": self.current_shout,
                "level": self.level,
                "carry_weight_pct": self.carry_weight_pct,
                "gold": self.gold,
                "visible_entities": [
                    {
                        "id": e.id,
                        "name": e.name,
                        "relationship": e.relationship,
                        "health_pct": e.health_pct,
                        "distance": e.distance,
                    }
                    for e in self.visible_entities
                ],
                "possible_actions": [
                    {
                        "name": a.name,
                        "target_required": a.target_required,
                        "description": a.description,
                    }
                    for a in self.possible_actions
                ],
            }
        )
        return base
