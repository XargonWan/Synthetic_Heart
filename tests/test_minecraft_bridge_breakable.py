"""The Minecraft bridge must not keep digging blocks that cannot be broken."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BRIDGE = (
    Path(__file__).resolve().parents[1]
    / "plugins/rift_vessel/minecraft/minecraft_bridge.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

_SCENARIOS = r"""
const out = {};
function mkBot(mode, cells) {
  return {
    game: { gameMode: mode },
    blockAt: (p) => cells[`${p.x},${p.y},${p.z}`] || { name: 'air', position: p },
    dig: async () => {},
  };
}
function pos(x, y, z) {
  return { x, y, z, clone() { return pos(x, y, z); }, offset(dx, dy, dz) { return pos(x + dx, y + dy, z + dz); } };
}
function stone(p, extra) { return Object.assign({ name: 'stone', position: p }, extra || {}); }

// game modes
bot = mkBot('adventure', {});
out.adventure = whyNotBreakable(stone(pos(0, 0, 0))).reason;
bot = mkBot('spectator', {});
out.spectator = whyNotBreakable(stone(pos(0, 0, 0))).reason;
bot = mkBot('survival', {});
out.survival_ok = whyNotBreakable(stone(pos(0, 0, 0)));

// unbreakable by nature, except where the player can break anything
const bedrock = (p) => ({ name: 'bedrock', position: p, diggable: false });
out.bedrock_survival = whyNotBreakable(bedrock(pos(1, 1, 1))).reason;
bot = mkBot('creative', {});
out.bedrock_creative = whyNotBreakable(bedrock(pos(1, 1, 1)));
bot = mkBot('survival', {});

(async () => {
  // a server that restores the block: dig "succeeds" but the cell is stone again
  const p = pos(5, 64, 5);
  const cells = { '5,64,5': stone(p) };
  bot = mkBot('survival', cells);
  out.reverted = (await digBlockVerified(stone(p))).broken;
  out.reverted_then = whyNotBreakable(stone(p)).reason;
  out.other_cell_untouched = whyNotBreakable(stone(pos(6, 64, 5)));

  // an honest break: the cell is air afterwards
  const q = pos(9, 64, 9);
  bot = mkBot('survival', {});
  out.broken = (await digBlockVerified(stone(q))).broken;
  out.broken_then = whyNotBreakable(stone(q));

  // sand falling into the dug cell is not a server revert
  const r = pos(2, 64, 2);
  bot = mkBot('survival', { '2,64,2': { name: 'sand', position: r }, '2,65,2': { name: 'sand', position: pos(2, 65, 2) } });
  out.falling = (await digBlockVerified({ name: 'sand', position: r })).broken;
  out.falling_then = whyNotBreakable({ name: 'sand', position: r });

  console.log(JSON.stringify(out));
})();
"""


def _run() -> dict:
    src = BRIDGE.read_text()
    start = src.index("// BEGIN breakable-blocks")
    end = src.index("// END breakable-blocks")
    region = src[start:end]
    script = (
        "let bot;\nconst sleep = (ms) => new Promise((r) => setTimeout(r, ms));\n"
        + region
        + _SCENARIOS
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_blocks_that_cannot_be_broken_are_refused_up_front_and_after_a_revert() -> None:
    out = _run()
    assert out["adventure"] == "game_mode" and out["spectator"] == "game_mode"
    assert out["survival_ok"] is None
    assert out["bedrock_survival"] == "unbreakable"
    assert out["bedrock_creative"] is None

    # The server put the block back: not broken, remembered, only for that cell.
    assert out["reverted"] is False
    assert out["reverted_then"] == "protected"
    assert out["other_cell_untouched"] is None

    # A real break leaves nothing behind to refuse.
    assert out["broken"] is True and out["broken_then"] is None

    # Gravity blocks refilling the cell are not mistaken for a protected area.
    assert out["falling"] is True and out["falling_then"] is None
