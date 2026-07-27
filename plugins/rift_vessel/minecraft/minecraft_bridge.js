#!/usr/bin/env node
/*
 * minecraft_bridge.js — Rift Vessel Minecraft bridge
 * ---------------------------------------------------------------
 * A Mineflayer <-> HTTP bridge that lets the SyntH Minecraft Vessel
 * connector drive an in-world bot and receive normalized world events.
 *
 * Design (see docs/rift_vessel.rst):
 *   - The Python connector (plugins/rift_vessel/minecraft/minecraft.py) talks to
 *     this process over plain HTTP on 127.0.0.1.
 *   - This bridge translates SyntH normalized actions -> Mineflayer commands
 *     (POST /cmd) and Mineflayer events -> normalized perception events, which
 *     it buffers for the connector to pull (POST /events long-poll style GET).
 *   - Offline-mode by default (PoC): no Microsoft/XBL auth.
 *
 * Endpoints:
 *   GET  /health            -> { ok, connected, username, environment, last_error }
 *   GET  /events            -> { events: [ {event_type, summary, actor, data} ] }
 *                              (drains and returns the buffered events)
 *   POST /cmd               -> { ok, detail, data }
 *                              body: { action, payload }
 *                              actions: say | move | look | use | attack |
 *                                       follow | unfollow | respawn | status |
 *                                       scan | skin |
 *                                       goto | mine | place | inventory | wander |
 *                                       craft
 *                              (skin payload: { command } — a server skin-plugin
 *                              chat command built by the Python connector)
 *                              (follow/goto/mine/wander require
 *                              mineflayer-pathfinder; when it is not installed
 *                              those actions fail gracefully)
 *   POST /connect           -> { ok, detail }   (re)connect to the server
 *   POST /disconnect        -> { ok }
 *
 * Env / args (all optional, sensible PoC defaults):
 *   BRIDGE_HOST         (default 127.0.0.1)
 *   BRIDGE_PORT         (default 8137)
 *   MC_SERVER_HOST      (default 127.0.0.1)
 *   MC_SERVER_PORT      (default 44383)
 *   MC_BOT_USERNAME     (default Synth)
 *   MC_AUTH             (default offline)
 *
 * This file lives next to the Minecraft connector
 * (plugins/rift_vessel/minecraft/) so all Minecraft Vessel assets stay
 * together. It is a Node.js runtime helper, not part of the Python import
 * graph — the provisioner (interface/minecraft_provisioner.py) copies it into
 * the bridge working directory and runs it as a subprocess.
 */

'use strict';

const http = require('http');

let mineflayer = null;
try {
  // eslint-disable-next-line global-require
  mineflayer = require('mineflayer');
} catch (err) {
  // Defer the hard failure until a connect is actually attempted so /health
  // still works and the provisioner can report a clear error.
  mineflayer = null;
}

// Optional: pathfinder enables entity-following and goal-based navigation
// (goto / wander). If the module is not installed the bridge still works —
// those actions just fail with a clear message.
let pathfinder = null;
try {
  // eslint-disable-next-line global-require
  pathfinder = require('mineflayer-pathfinder');
} catch (err) {
  pathfinder = null;
}

// Optional: minecraft-data powers pathfinder Movements (block costs, tool
// selection). Loaded lazily per-version on login. Absent => Movements uses
// its built-in defaults, which is still functional.
let minecraftData = null;
try {
  // eslint-disable-next-line global-require
  minecraftData = require('minecraft-data');
} catch (err) {
  minecraftData = null;
}

// Optional: mineflayer-auto-eat handles hunger *reflexively* — the bot eats a
// food item on its own when its food level drops, with no manual verb and no
// LLM turn. This is the self-preservation answer to starvation (see AGENTS.md
// §5c / self-preservation): purely structural, driven by the bot's numeric
// food level. Absent => the bot simply won't auto-eat (it never starves faster,
// it just won't top up on its own). Loaded per-bot on login.
let autoEat = null;
try {
  // eslint-disable-next-line global-require
  autoEat = require('mineflayer-auto-eat');
} catch (err) {
  autoEat = null;
}

// Optional: mineflayer-collectblock composes navigate → dig → pick up the drop
// into a single verified behaviour, so a gather action actually ends with the
// item in the inventory (a raw bot.dig leaves the drop on the ground and the
// gather often "succeeds" while collecting nothing). Absent => the 'mine' verb
// falls back to a raw dig. Loaded per-bot on login.
let collectBlock = null;
try {
  // eslint-disable-next-line global-require
  collectBlock = require('mineflayer-collectblock');
} catch (err) {
  collectBlock = null;
}

// The pathfinder Movements instance configured for the connected world, set on
// login. Kept module-level so goto/wander reuse it.
let botMovements = null;

// Persistent wander heading (radians). Free exploration keeps a broadly
// consistent bearing across legs so the bot treks *away* rather than circling
// back on itself ("si muove circolarmente"). Each leg only nudges the heading
// by a small random drift; it is reset to a fresh random bearing on each new
// login so a new session doesn't inherit the previous direction.
let wanderHeading = null;

const CFG = {
  bridgeHost: process.env.BRIDGE_HOST || '127.0.0.1',
  bridgePort: parseInt(process.env.BRIDGE_PORT || '8137', 10),
  serverHost: process.env.MC_SERVER_HOST || '127.0.0.1',
  serverPort: parseInt(process.env.MC_SERVER_PORT || '44383', 10),
  username: process.env.MC_BOT_USERNAME || 'Synth',
  auth: process.env.MC_AUTH || 'offline',
  // Optional protocol-version pin. Empty string => let Mineflayer auto-detect
  // the server version. Set MC_VERSION (via MINECRAFT_SERVER_VERSION) when the
  // server announces a protocol the bundled minecraft-data doesn't recognise
  // ("No data available for version X").
  version: (process.env.MC_VERSION || '').trim(),
};

const ENVIRONMENT = 'minecraft';
const EVENT_BUFFER_MAX = 500;

/** @type {Array<object>} */
let eventBuffer = [];
/** @type {import('mineflayer').Bot | null} */
let bot = null;
let connected = false;
// Last connection failure reason (spawn error, kick, disconnect before spawn,
// version mismatch, ...). Surfaced via /connect and /health so the Python
// connector — and ultimately Synth — can tell the requester WHY it failed.
let lastError = null;
// Resolved once the current connect attempt reaches a terminal state (either a
// successful spawn or a failure). connectBot awaits this so /connect returns
// the real outcome instead of an optimistic "connecting".
/** @type {((result: {ok: boolean, detail: string}) => void) | null} */
let connectResolver = null;
// Some servers (auth/whitelist plugins, connection throttling, proxy layers
// like Velocity/BungeeCord) drop the FIRST handshake before the bot spawns and
// then accept a retry. Track whether the in-flight connect attempt ever reached
// 'spawn' so an 'end'/'error' that fires BEFORE spawn can transparently retry
// createBot instead of failing the whole /connect. Reset at the start of each
// connectBot() call.
let spawnedThisAttempt = false;
let connectRetriesLeft = 0;
/** @type {(() => void) | null} */
let retryConnectBot = null;
// The mineflayer bot options of the most recent successful connect. Kept so the
// POST-spawn auto-reconnect (see scheduleReconnect) can recreate the bot toward
// the SAME server after the world drops us — without the Python provisioner
// having to reap+respawn the whole Node process (which races the port and
// crashes the fresh bridge with EADDRINUSE).
/** @type {object | null} */
let lastBotOpts = null;
// Set true when the disconnect was requested by us (disconnectBot / shutdown /
// a fresh connect superseding the old bot) so the post-spawn auto-reconnect
// does NOT fight an intentional teardown. Reset at the start of connectBot.
let intentionalDisconnect = false;
// Handle for the in-flight post-spawn reconnect backoff timer.
/** @type {ReturnType<typeof setTimeout> | null} */
let reconnectTimer = null;
// How long to wait before an in-process post-spawn reconnect attempt.
const POST_SPAWN_RECONNECT_DELAY_MS = 3000;
// Interval handle for the post-login presence poller (see wireBotEvents). It
// detects "in the world" via bot.entity.position for servers that never emit a
// reliable 'spawn' event. Cleared on spawn, settle, retry and disconnect.
/** @type {ReturnType<typeof setInterval> | null} */
let presencePollTimer = null;

// Tracks the pathfinding navigation currently in flight so repeated `goto`
// commands toward the SAME destination are idempotent. Without this, the
// autonomy motor tick (which re-issues `goto` every few seconds) would reset
// the pathfinder's goal on every tick — mineflayer aborts the in-progress walk
// with "The goal was changed before it could be completed!" and the bot never
// actually travels anywhere. When a new goto arrives with the same navKey while
// a walk is active we simply report "already navigating" and let the existing
// walk continue. A different destination cleanly supersedes the old one.
/** @type {{ key: string, promise: Promise<any> } | null} */
let activeNav = null;

// Tracks the most recent melee swing near the bot so a `damage` event can be
// attributed to an attacker. Mineflayer's high-level `entityHurt` only reports
// WHICH entity was hurt, never WHO hit it — so we correlate the hurt with the
// closest entity that recently swung its arm (structural, no keyword logic).
/** @type {{ id: number, at: number } | null} */
let lastSwing = null;
const SWING_ATTRIBUTION_WINDOW_MS = 1500;
const ATTACKER_MAX_DISTANCE = 6;

// Classify an entity as an attacker source without keyword matching: a mob is
// hostile game logic, a real player is a person. Falls back to a neutral
// "entity" when the structural type is unknown.
function classifyAttacker(entity) {
  if (!entity) return 'entity';
  if (entity.type === 'player' || entity.username) return 'player';
  if (entity.type === 'mob') return 'mob';
  return String(entity.type || 'entity');
}

// Passive/tameable mobs whose structural type is "mob" but which are NOT a
// self-preservation threat. These are game-mechanic categories (kind/category
// exposed by mineflayer's entity metadata), not user-facing words, so this is
// structural game logic — not keyword matching on human language.
const _NON_HOSTILE_MOB_CATEGORIES = new Set([
  'Passive mobs',
  'Tameable mobs',
  'Water animals',
  'NPCs',
]);

// Golems the bot spawned/owns are allies, not threats. These are canonical
// Minecraft entity ids (game enum), not human language.
const _FRIENDLY_MOB_NAMES = new Set(['iron_golem', 'snow_golem']);

// Structural hostility flag for the self-preservation reflex. A creature is a
// threat when its game-logic type is "mob" AND it is not a passive/tameable
// category and not a friendly golem. Purely structural (entity.type /
// entity.kind / canonical id) — never a keyword scan of display text, so it
// works regardless of client language.
function isHostileEntity(entity) {
  if (!entity) return false;
  if (entity.type !== 'mob') return false;
  const id = entity.name ? String(entity.name).toLowerCase() : '';
  if (_FRIENDLY_MOB_NAMES.has(id)) return false;
  // mineflayer exposes a coarse category on `kind` / `entityType` metadata for
  // many mobs; when present, exclude the passive/tameable buckets.
  const category = entity.kind || (entity.metadata && entity.metadata.category);
  if (category && _NON_HOSTILE_MOB_CATEGORIES.has(String(category))) {
    return false;
  }
  return true;
}

// Resolve who most likely dealt the damage: prefer the entity that swung its
// arm within the attribution window (and is still within melee range), else
// the nearest hostile mob or player. Returns null when no plausible source is
// found (e.g. fall/environmental damage).
function resolveAttacker() {
  if (!bot || !bot.entity || !bot.entities) return null;
  const self = bot.entity;
  const now = Date.now();
  // 1) A recent swinger, if it is still close.
  if (lastSwing && now - lastSwing.at <= SWING_ATTRIBUTION_WINDOW_MS) {
    const swinger = bot.entities[lastSwing.id];
    if (swinger && swinger !== self && swinger.position) {
      const dist = self.position.distanceTo(swinger.position);
      if (dist <= ATTACKER_MAX_DISTANCE) {
        return { entity: swinger, distance: Math.round(dist * 10) / 10 };
      }
    }
  }
  // 2) Fallback: the nearest mob or player within melee range.
  let best = null;
  for (const e of Object.values(bot.entities)) {
    if (!e || e === self || !e.position) continue;
    if (e.type !== 'mob' && e.type !== 'player' && !e.username) continue;
    const dist = self.position.distanceTo(e.position);
    if (dist > ATTACKER_MAX_DISTANCE) continue;
    if (!best || dist < best.distance) {
      best = { entity: e, distance: Math.round(dist * 10) / 10 };
    }
  }
  return best;
}

function settleConnect(result) {
  if (connectResolver) {
    const resolve = connectResolver;
    connectResolver = null;
    resolve(result);
  }
}

function log(...args) {
  // eslint-disable-next-line no-console
  console.log('[mc-bridge]', ...args);
}

function pushEvent(evt) {
  eventBuffer.push(evt);
  if (eventBuffer.length > EVENT_BUFFER_MAX) {
    eventBuffer = eventBuffer.slice(-EVENT_BUFFER_MAX);
  }
}

function drainEvents() {
  const out = eventBuffer;
  eventBuffer = [];
  return out;
}

function botPosition() {
  if (!bot || !bot.entity || !bot.entity.position) return null;
  const p = bot.entity.position;
  return { x: p.x, y: p.y, z: p.z };
}

function roundVec(v) {
  if (!v) return null;
  return { x: Math.round(v.x), y: Math.round(v.y), z: Math.round(v.z) };
}

// Mineflayer 'kicked' reasons arrive as a chat component (object), a JSON
// string, or a plain string. String(reason) on an object yields the useless
// "[object Object]", so flatten it to readable text: prefer .text, then any
// nested .extra[].text, then a JSON dump, falling back to String().
function reasonToString(reason) {
  if (reason == null) return 'unknown';
  if (typeof reason === 'string') {
    // Servers often send a JSON-encoded chat component as a string.
    try {
      const parsed = JSON.parse(reason);
      const flat = reasonToString(parsed);
      if (flat && flat !== 'unknown') return flat;
    } catch (e) {
      /* not JSON — use the raw string */
    }
    return reason;
  }
  if (typeof reason === 'object') {
    const parts = [];
    if (typeof reason.text === 'string') parts.push(reason.text);
    if (Array.isArray(reason.extra)) {
      for (const child of reason.extra) parts.push(reasonToString(child));
    }
    const joined = parts.filter(Boolean).join('').trim();
    if (joined) return joined;
    try {
      return JSON.stringify(reason);
    } catch (e) {
      return String(reason);
    }
  }
  return String(reason);
}

// --- World perception helpers ---------------------------------------------
// These build the world-agnostic "what is around me" snapshot the Python
// connector maps into WorldState.extra so Synth can decide how to play.
// They are purely descriptive (no interaction) and fully guarded so a bad
// read never breaks a status/scan reply.

function nearbyEntities(maxCount, maxDistance) {
  if (!bot || !bot.entities || !bot.entity) return [];
  const self = bot.entity;
  const out = [];
  for (const e of Object.values(bot.entities)) {
    if (!e || e === self || !e.position) continue;
    const dist = self.position.distanceTo(e.position);
    if (maxDistance && dist > maxDistance) continue;
    out.push({
      name: e.username || e.name || e.displayName || 'entity',
      kind: e.type || (e.username ? 'player' : 'entity'),
      // mob | player | object | ... (mineflayer entity.type)
      distance: Math.round(dist * 10) / 10,
      position: roundVec(e.position),
      // Structural self-preservation flag (see isHostileEntity): true for a
      // threatening mob, false for players/passive creatures/friendly golems.
      // Purely game-logic based, no keyword scan of display names.
      hostile: isHostileEntity(e),
    });
  }
  out.sort((a, b) => a.distance - b.distance);
  return out.slice(0, maxCount || 12);
}

// Blocks of interest around the bot. We do NOT dump every block (too noisy) —
// we surface only blocks the bot can reach and that carry an interaction
// affordance (blocks with a name), sampled on a coarse grid to stay cheap.
function nearbyBlocks(radius, maxCount) {
  if (!bot || !bot.entity || typeof bot.blockAt !== 'function') return [];
  const r = Math.min(Math.max(parseInt(radius, 10) || 4, 1), 16);
  const origin = bot.entity.position.floored();
  const seen = new Set();
  const out = [];
  for (let dx = -r; dx <= r; dx += 1) {
    for (let dy = -2; dy <= 2; dy += 1) {
      for (let dz = -r; dz <= r; dz += 1) {
        const pos = origin.offset(dx, dy, dz);
        let block = null;
        try {
          block = bot.blockAt(pos);
        } catch (e) {
          block = null;
        }
        if (!block || !block.name || block.name === 'air') continue;
        // Deduplicate by block name to give a compact "what is around" view
        // rather than thousands of stone/dirt cells.
        if (seen.has(block.name)) continue;
        seen.add(block.name);
        const dist = bot.entity.position.distanceTo(block.position);
        out.push({
          name: block.name,
          distance: Math.round(dist * 10) / 10,
          position: roundVec(block.position),
        });
        if (out.length >= (maxCount || 24)) return out;
      }
    }
  }
  out.sort((a, b) => a.distance - b.distance);
  return out;
}

function botInventory() {
  if (!bot || !bot.inventory || typeof bot.inventory.items !== 'function') {
    return [];
  }
  try {
    return bot.inventory.items().map((it) => ({
      name: it.name,
      display_name: it.displayName || it.name,
      count: it.count,
      slot: it.slot,
    }));
  } catch (e) {
    return [];
  }
}

// Total quantity of an item (by canonical id) currently held, summed across
// stacks. Structural: matches the game item id exactly, never human text. Used
// to verify a gather actually landed the drop in the inventory (compute the
// before/after delta around a dig/collect).
function inventoryCountByName(name) {
  const wanted = name != null ? String(name).trim().toLowerCase() : '';
  if (!wanted) return 0;
  let total = 0;
  for (const it of botInventory()) {
    if (it.name && it.name.toLowerCase() === wanted) total += it.count || 0;
  }
  return total;
}

// Map of item id -> total held count, summed across stacks. A structural
// snapshot of the whole inventory used to measure what a gather yielded
// regardless of which drop id a block produces (e.g. stone -> cobblestone).
function inventoryTotals() {
  const totals = {};
  for (const it of botInventory()) {
    if (!it.name) continue;
    totals[it.name] = (totals[it.name] || 0) + (it.count || 0);
  }
  return totals;
}

// Positive item-count delta between two inventory snapshots (after - before),
// keeping only ids that increased. Returns { itemId: gainedCount }.
function inventoryDelta(before, after) {
  const gained = {};
  for (const name of Object.keys(after || {})) {
    const diff = (after[name] || 0) - ((before && before[name]) || 0);
    if (diff > 0) gained[name] = diff;
  }
  return gained;
}

// Small awaitable delay used to let physics/pickup settle after a raw dig so
// the inventory delta reflects the collected drop.
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function timeOfDay() {
  if (!bot || !bot.time) return null;
  const t = typeof bot.time.timeOfDay === 'number' ? bot.time.timeOfDay : null;
  // Minecraft day = 24000 ticks; 0..12000 is daytime.
  const isDay = t == null ? null : t >= 0 && t < 12000;
  return { time_of_day: t, is_day: isDay };
}

// Name of the block at a given offset from the bot's feet. Guarded — returns
// null when there is no bot/block. Used for the self-preservation reflex
// (drowning / standing in lava or fire). Structural: it reads canonical block
// ids (game enum), never human text.
function blockNameAt(dx, dy, dz) {
  if (!bot || !bot.entity || typeof bot.blockAt !== 'function') return null;
  try {
    const pos = bot.entity.position.floored().offset(dx, dy, dz);
    const block = bot.blockAt(pos);
    return block && block.name ? block.name : null;
  } catch (e) {
    return null;
  }
}

// Build the full world snapshot shared by 'status' and 'scan'. Every field is
// guarded so a partial read still returns a useful object.
function worldSnapshot(opts) {
  const options = opts || {};
  const radius = options.radius;
  const maxEntities = options.max_entities;
  const maxBlocks = options.max_blocks;
  const time = timeOfDay();
  return {
    connected,
    username: bot ? bot.username || CFG.username : CFG.username,
    health: bot && typeof bot.health === 'number' ? bot.health : null,
    food: bot && typeof bot.food === 'number' ? bot.food : null,
    position: botPosition(),
    dimension: bot && bot.game ? bot.game.dimension || null : null,
    game_mode: bot && bot.game ? bot.game.gameMode || null : null,
    time_of_day: time ? time.time_of_day : null,
    is_day: time ? time.is_day : null,
    entities: nearbyEntities(maxEntities, radius),
    blocks: nearbyBlocks(radius, maxBlocks),
    inventory: botInventory(),
    // --- Self-preservation telemetry (all guarded, null when unavailable) ---
    // Remaining air ticks (0..20 on vanilla). Drops while submerged; the motor
    // reflex surfaces the bot before it hits 0 and starts drowning.
    oxygen:
      bot && typeof bot.oxygenLevel === 'number' ? bot.oxygenLevel : null,
    // Whether the bot's body is currently in water (structural physics flag).
    is_in_water:
      bot && bot.entity && typeof bot.entity.isInWater === 'boolean'
        ? bot.entity.isInWater
        : null,
    // Whether the bot is alive. Mineflayer sets health to 0 and fires 'death'
    // on death; expose a simple structural flag for the reflex.
    is_alive:
      bot && typeof bot.health === 'number' ? bot.health > 0 : null,
    // Canonical block ids at the bot's feet and head — lets the reflex detect
    // standing in lava/fire or having its head underwater (drowning).
    block_feet: blockNameAt(0, 0, 0),
    block_head: blockNameAt(0, 1, 0),
  };
}

// Resolve a target entity from a free-form 'target' payload value. When a
// name is given, match a player/entity by (display) name; otherwise fall back
// to the nearest entity. Returns null when nothing suitable is around.
function resolveTargetEntity(target) {
  if (!bot || !bot.entities) return null;
  const wanted = target != null ? String(target).trim().toLowerCase() : '';
  const entities = Object.values(bot.entities).filter(
    (e) => e && e !== bot.entity && e.position,
  );
  if (wanted) {
    const named = entities.find((e) => {
      const uname = e.username ? String(e.username).toLowerCase() : '';
      const ename = e.name ? String(e.name).toLowerCase() : '';
      return uname === wanted || ename === wanted;
    });
    if (named) return named;
  }
  // Nearest entity fallback.
  let nearest = null;
  let best = Infinity;
  for (const e of entities) {
    const d = bot.entity.position.distanceTo(e.position);
    if (d < best) {
      best = d;
      nearest = e;
    }
  }
  return nearest;
}

// Resolve the nearest block matching a free-form 'target' name (e.g. the block
// name reported by nearbyBlocks/scan). Returns the block object or null. Purely
// structural: the caller supplies the exact block name to look for; the bridge
// never interprets what a name means.
function resolveTargetBlock(target, maxDistance) {
  if (!bot || typeof bot.findBlock !== 'function') return null;
  const wanted = target != null ? String(target).trim().toLowerCase() : '';
  if (!wanted) return null;
  try {
    const blocks = bot.findBlocks({
      matching: (blk) => blk && blk.name && blk.name.toLowerCase() === wanted,
      maxDistance: Math.min(Math.max(parseInt(maxDistance || '32', 10) || 32, 1), 64),
      count: 1,
    });
    if (blocks && blocks.length) {
      return bot.blockAt(blocks[0]);
    }
  } catch (e) {
    /* fall through */
  }
  return null;
}

// Run pathfinder toward a goal and resolve when it arrives, errors, or times
// out. Emits an 'arrival' perception event on success. Fully guarded so a
// missing pathfinder or an unreachable goal degrades gracefully.
async function navigateToGoal(goal, timeoutMs, navKey) {
  if (!pathfinder || !bot.pathfinder || typeof bot.pathfinder.goto !== 'function') {
    return { ok: false, detail: 'navigation unavailable (pathfinder not loaded)' };
  }
  // Idempotent re-issue: a goto toward a destination we are already walking to
  // is a no-op so the in-progress walk is never interrupted. The autonomy motor
  // tick relies on this — it re-issues the same goto every few seconds and must
  // NOT reset the pathfinder each time, or the bot never leaves the spot.
  if (navKey && activeNav && activeNav.key === navKey) {
    return {
      ok: true,
      detail: 'already navigating',
      data: { position: botPosition(), navigating: true },
    };
  }
  const budget = Math.min(Math.max(parseInt(timeoutMs || '30000', 10) || 30000, 1000), 120000);
  const run = (async () => {
    try {
      await Promise.race([
        bot.pathfinder.goto(goal),
        new Promise((_, reject) => setTimeout(() => reject(new Error('navigation timed out')), budget)),
      ]);
      // No 'arrival' perception event is emitted: the autonomy motor tick
      // re-issues the same goto every few seconds, so an arrival event floods
      // the Rift Vessel activity log / WebUI. Arrival is still returned to the
      // caller for control flow, just not surfaced as a logged perception.
      return { ok: true, detail: 'arrived', data: { position: botPosition() } };
    } catch (err) {
      try {
        if (bot.pathfinder && typeof bot.pathfinder.setGoal === 'function') {
          bot.pathfinder.setGoal(null);
        }
      } catch (e) {
        /* ignore */
      }
      return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
    }
  })();
  if (navKey) {
    // Keyed (autonomous travel) navigation is fire-and-forget: the walk to a
    // far destination can take much longer than the motor tick interval, so we
    // start it in the background and return immediately. Subsequent identical
    // gotos short-circuit above; a different destination supersedes this one.
    activeNav = { key: navKey, promise: run };
    run.finally(() => {
      if (activeNav && activeNav.promise === run) {
        activeNav = null;
      }
    });
    run.catch(() => {
      /* background walk failure already handled; avoid unhandled rejection */
    });
    return { ok: true, detail: 'navigation started', data: { position: botPosition(), navigating: true } };
  }
  return await run;
}

function wireBotEvents(b) {
  // Shared "we are truly in the world" settlement. Called from both the
  // canonical 'spawn' event AND the post-login presence poller below, because
  // some servers/proxies (Velocity/BungeeCord and heavily-plugged Spigot setups)
  // log the bot in, deliver world state (position, chat, teleports) — but never
  // emit a reliable 'spawn' event. Idempotent via spawnedThisAttempt.
  const markSpawned = (source) => {
    if (spawnedThisAttempt) return;
    connected = true;
    lastError = null;
    spawnedThisAttempt = true;
    // Fresh session → fresh exploration bearing (see the wander case).
    wanderHeading = null;
    if (presencePollTimer) {
      clearInterval(presencePollTimer);
      presencePollTimer = null;
    }
    log(`in the world (${source})`);
    settleConnect({ ok: true, detail: 'spawned' });
  };

  b.on('spawn', () => {
    // 'spawn' is the authoritative "we are actually in the world" signal.
    markSpawned('spawn');
  });

  b.on('login', () => {
    connected = true;
    log('logged in as', b.username);
    // Presence fallback: poll for a valid entity position. Once the bot has an
    // entity with coordinates it IS embodied in the world, regardless of whether
    // the server bothered to emit 'spawn'. This is the fix for servers where
    // 'spawn' never fires but the bot clearly loaded (receives teleports/chat),
    // causing a false "timed out waiting to enter the world". Structural probe,
    // no keyword logic; cleared on spawn/settle. Best-effort — guarded.
    if (presencePollTimer) {
      clearInterval(presencePollTimer);
      presencePollTimer = null;
    }
    presencePollTimer = setInterval(() => {
      try {
        if (spawnedThisAttempt || !bot) {
          if (presencePollTimer) {
            clearInterval(presencePollTimer);
            presencePollTimer = null;
          }
          return;
        }
        if (bot.entity && bot.entity.position) {
          markSpawned('presence');
        }
      } catch (e) {
        /* ignore — the poller must never throw */
      }
    }, 500);
    if (pathfinder && pathfinder.pathfinder) {
      try {
        b.loadPlugin(pathfinder.pathfinder);
        // Configure Movements so goto/wander can pathfind with sensible block
        // costs and tool selection. Best-effort: on any failure the pathfinder
        // still works with its built-in defaults.
        try {
          const version = b.version;
          const mcData = minecraftData && version ? minecraftData(version) : null;
          if (mcData && pathfinder.Movements) {
            botMovements = new pathfinder.Movements(b, mcData);
            if (b.pathfinder && typeof b.pathfinder.setMovements === 'function') {
              b.pathfinder.setMovements(botMovements);
            }
          }
        } catch (e2) {
          log('movements setup failed:', e2 && e2.message ? e2.message : e2);
          botMovements = null;
        }
      } catch (e) {
        log('pathfinder load failed:', e && e.message ? e.message : e);
      }
    }
    // Reflexive hunger handling: load mineflayer-auto-eat so the bot tops up
    // on its own when its food level drops. Best-effort — a missing dep or a
    // load failure simply means no auto-eat; it never breaks the session.
    if (autoEat) {
      try {
        // The plugin exports either the loader directly or as `.plugin`
        // (package layout varies across versions); pick whichever is callable.
        const autoEatPlugin =
          typeof autoEat === 'function'
            ? autoEat
            : autoEat && typeof autoEat.plugin === 'function'
              ? autoEat.plugin
              : autoEat && typeof autoEat.loader === 'function'
                ? autoEat.loader
                : null;
        if (autoEatPlugin) {
          b.loadPlugin(autoEatPlugin);
          // Configure sensible defaults if the plugin exposes options. Eat
          // before starvation bites; never interrupt combat/movement mid-swing.
          if (b.autoEat && typeof b.autoEat === 'object') {
            try {
              b.autoEat.enableAuto = true;
              if (b.autoEat.options && typeof b.autoEat.options === 'object') {
                b.autoEat.options.priority = 'foodPoints';
                b.autoEat.options.startAt = 16;
                b.autoEat.options.bannedFood = [];
              }
            } catch (e3) {
              log('auto-eat options setup skipped:', e3 && e3.message ? e3.message : e3);
            }
          }
          log('auto-eat loaded');
        }
      } catch (e) {
        log('auto-eat load failed:', e && e.message ? e.message : e);
      }
    }
    // Verified gathering: load mineflayer-collectblock so the 'mine' verb walks
    // to the block, digs it, and picks up the drop as one behaviour. Best-effort
    // — a missing dep or load failure simply means 'mine' falls back to a raw
    // dig (see the 'mine' case). The plugin exports the loader either directly
    // or as `.plugin`; pick whichever is callable.
    if (collectBlock) {
      try {
        const collectBlockPlugin =
          typeof collectBlock === 'function'
            ? collectBlock
            : collectBlock && typeof collectBlock.plugin === 'function'
              ? collectBlock.plugin
              : collectBlock && typeof collectBlock.loader === 'function'
                ? collectBlock.loader
                : null;
        if (collectBlockPlugin) {
          b.loadPlugin(collectBlockPlugin);
          log('collectblock loaded');
        }
      } catch (e) {
        log('collectblock load failed:', e && e.message ? e.message : e);
      }
    }
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'spawn',
      summary: `Embodied in the world as ${b.username}`,
      actor: b.username,
      data: { position: botPosition() },
    });
  });

  b.on('chat', (username, message) => {
    log('chat event:', JSON.stringify({ username, message }));
    if (username === b.username) return;
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'chat',
      summary: `${username}: ${message}`,
      actor: username,
      data: { message },
    });
  });

  // Diagnostic catch-all: some servers deliver player chat through a custom
  // formatted system message (chat plugins, LuckPerms prefixes, etc.) that the
  // high-level 'chat' event does not decode into (username, message). Log the
  // raw rendered text of every incoming message so we can see WHY 'chat' is not
  // firing. This is a structural probe (no keyword logic), diagnostic only.
  b.on('messagestr', (message, position) => {
    log('messagestr:', JSON.stringify({ position, message: String(message).slice(0, 200) }));
  });

  b.on('playerJoined', (player) => {
    if (!player || player.username === b.username) return;
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'proximity',
      summary: `${player.username} joined the world`,
      actor: player.username,
      data: {},
    });
  });

  // Record every melee swing so `entityHurt` can attribute the hit to a
  // source. Mineflayer fires this for any entity that swings its arm.
  b.on('entitySwingArm', (entity) => {
    if (!entity || (bot && entity === bot.entity)) return;
    lastSwing = { id: entity.id, at: Date.now() };
  });

  b.on('entityHurt', (entity) => {
    if (!bot || !entity || entity !== bot.entity) return;
    const attacker = resolveAttacker();
    const data = { health: bot.health };
    let summary = 'Took damage';
    let actor = b.username;
    if (attacker && attacker.entity) {
      const src = classifyAttacker(attacker.entity);
      const name =
        attacker.entity.username ||
        attacker.entity.name ||
        attacker.entity.displayName ||
        src;
      data.attacker = {
        name,
        source: src,
        distance: attacker.distance,
        position: roundVec(attacker.entity.position),
      };
      actor = name;
      summary = `Took damage from ${name}`;
    }
    // Clear the swing so a later unrelated hurt is not mis-attributed.
    lastSwing = null;
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'damage',
      summary,
      actor,
      data,
    });
  });

  b.on('death', () => {
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'death',
      summary: 'Died in the world. Respawn to come back to life.',
      actor: b.username,
      data: { dead: true },
    });
  });

  b.on('kicked', (reason) => {
    connected = false;
    // Some servers KICK the first handshake before the bot ever spawns and
    // then accept a retry (observed: first attempt "kicked: [object Object]",
    // second attempt logs in cleanly). Mirror the pre-spawn retry logic of the
    // 'end' handler: if this kick fired before spawn and we still have retries
    // budgeted, transparently retry createBot instead of failing the whole
    // /connect. The paired 'end' event usually fires too, but a kick can be
    // terminal on its own, so drive the retry from here as well.
    const reasonStr = reasonToString(reason);
    lastError = `kicked: ${reasonStr}`;
    if (!spawnedThisAttempt && connectRetriesLeft > 0 && retryConnectBot) {
      connectRetriesLeft -= 1;
      log(`kicked before spawn — retrying (${connectRetriesLeft} attempt(s) left): ${reasonStr}`);
      retryConnectBot();
      return;
    }
    settleConnect({ ok: false, detail: lastError });
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'disconnect',
      summary: `Kicked: ${reasonStr}`,
      actor: b.username,
      data: { reason: reasonStr },
    });
    // POST-spawn kick: the world dropped us AFTER we were embodied. Reconnect
    // in-process so the HTTP bridge (and thus the Python session) survives.
    scheduleReconnect(`kicked: ${reasonStr}`);
  });

  b.on('end', () => {
    connected = false;
    log('connection ended');
    // Some servers close the FIRST handshake before the bot ever spawns and
    // then accept a retry. If this 'end' fired before spawn and we still have
    // retries budgeted, transparently retry createBot instead of failing the
    // whole /connect — this matches the observed server behaviour where the
    // second attempt logs in cleanly.
    if (!spawnedThisAttempt && connectRetriesLeft > 0 && retryConnectBot) {
      connectRetriesLeft -= 1;
      log(
        `connection ended before spawn — retrying (${connectRetriesLeft} attempt(s) left)`
      );
      retryConnectBot();
      return;
    }
    // If the connection ended before we ever spawned, treat it as a failed
    // connect so /connect can report a real reason (fall back to lastError).
    settleConnect({ ok: false, detail: lastError || 'connection ended before spawn' });
    // POST-spawn end: the world dropped us AFTER we were embodied (spawn had
    // already fired, so this is not a first-handshake failure). Reconnect
    // in-process so the HTTP bridge — and the Python vessel session that reads
    // /health — survives the drop instead of the provisioner reaping and
    // respawning the whole Node process (which races the port -> EADDRINUSE).
    scheduleReconnect('connection ended');
  });

  b.on('error', (err) => {
    const msg = err && err.message ? err.message : String(err);
    lastError = msg;
    log('bot error:', msg);
    // Mirror the pre-spawn retry logic: a transient socket/handshake error on
    // the first attempt should not fail the whole connect if we still have
    // retries budgeted. The paired 'end' event will drive the actual retry, so
    // here we only record the reason and let 'end' decide.
    if (!spawnedThisAttempt && connectRetriesLeft > 0 && retryConnectBot) {
      return;
    }
    settleConnect({ ok: false, detail: msg });
  });
}

// How long to wait for the bot to actually reach the world (spawn) or fail
// after createBot before /connect gives up and reports a timeout. This budget
// covers the WHOLE connect (including pre-spawn retries below).
//
// Raised from 30000 to 90000: some servers (e.g. proxied/heavily-plugged
// setups, or a distant host) take noticeably longer than 30s to complete the
// login+world-load handshake. When the budget was 30s the /connect timed out,
// the Python connector reported connect_failed and CLOSED the freshly-opened
// vessel session — yet the bot then spawned a few seconds later and stayed
// connected, leaving an ORPHANED bridge with no driven session (no motor tick,
// no will beat). 90s gives slow servers ample headroom while the presence
// poller (see wireBotEvents) still settles the connect the instant the bot is
// embodied, so a fast server is not slowed down.
const CONNECT_TIMEOUT_MS = 90000;
// How many extra createBot attempts to make when the server closes the
// handshake before the bot spawns (see the 'end' handler in wireBotEvents).
const CONNECT_MAX_RETRIES = 3;
// Backoff between pre-spawn retries.
const CONNECT_RETRY_DELAY_MS = 1500;

// Re-create the mineflayer bot IN-PROCESS after a POST-spawn drop (the world
// kicked us / the connection ended after we were already embodied). This keeps
// the HTTP bridge (:8137) alive and `connected` recoverable, so the Python
// vessel session — which polls /health — stays alive across a transient server
// drop instead of the provisioner reaping and respawning the whole Node process
// (which races the listening socket and crashes the fresh bridge with
// EADDRINUSE). Unbounded but backed-off; a genuine disconnect (disconnectBot /
// shutdown / a superseding connect) sets intentionalDisconnect to stop it.
function scheduleReconnect(reason) {
  if (intentionalDisconnect) return;
  if (!lastBotOpts) return;
  if (reconnectTimer) return; // one reconnect in flight at a time
  log(`post-spawn drop (${reason}) — auto-reconnecting in ${POST_SPAWN_RECONNECT_DELAY_MS} ms`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (intentionalDisconnect || !lastBotOpts) return;
    if (presencePollTimer) {
      clearInterval(presencePollTimer);
      presencePollTimer = null;
    }
    if (bot) {
      try {
        bot.removeAllListeners();
        bot.quit();
      } catch (e) {
        /* ignore */
      }
      bot = null;
    }
    connected = false;
    spawnedThisAttempt = false;
    try {
      bot = mineflayer.createBot(lastBotOpts);
      wireBotEvents(bot);
      log(
        `auto-reconnecting to ${lastBotOpts.host}:${lastBotOpts.port} as ${lastBotOpts.username}`
      );
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      lastError = msg;
      log('auto-reconnect createBot failed:', msg);
      // Retry again after the backoff; the world may still be coming back.
      scheduleReconnect(`retry after createBot error: ${msg}`);
    }
  }, POST_SPAWN_RECONNECT_DELAY_MS);
}

function connectBot(overrides) {
  if (!mineflayer) {
    return Promise.resolve({ ok: false, detail: 'mineflayer module not installed' });
  }
  if (bot) {
    try {
      bot.quit();
    } catch (e) {
      /* ignore */
    }
    bot = null;
    connected = false;
  }
  lastError = null;
  connectRetriesLeft = CONNECT_MAX_RETRIES;
  // A fresh connect is an intentional (re)start: clear any in-flight post-spawn
  // auto-reconnect and re-arm it for the new bot.
  intentionalDisconnect = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  // Per-connect overrides let Synth target a different Minecraft server than
  // the plugin defaults (CFG, seeded from MINECRAFT_SERVER_* env). Any field
  // omitted falls back to the configured default.
  const opts = overrides && typeof overrides === 'object' ? overrides : {};
  const host = opts.host ? String(opts.host) : CFG.serverHost;
  const port = opts.port ? parseInt(String(opts.port), 10) || CFG.serverPort : CFG.serverPort;
  const username = opts.username ? String(opts.username) : CFG.username;
  // Per-connect version override falls back to the configured CFG.version;
  // an empty value means "auto-detect" (omit the option entirely).
  const version = opts.version ? String(opts.version).trim() : CFG.version;
  try {
    const botOpts = {
      host,
      port,
      username,
      auth: CFG.auth,
      // Keep the loaded-chunk footprint small. Mineflayer caches every chunk
      // the server streams; on a normal render distance this grows the Node
      // heap unbounded and the process OOM-crashes after a couple of minutes
      // (observed: ~3.9 GB then "Ineffective mark-compacts near heap limit").
      // The bot does not need to see far to play, so pin the smallest view.
      viewDistance: 'tiny',
    };
    if (version) {
      botOpts.version = version;
    }
    // Remember the resolved options so a POST-spawn drop can reconnect to the
    // SAME server in-process (see scheduleReconnect).
    lastBotOpts = botOpts;
    // Await the real outcome: resolve on the first terminal event (spawn =>
    // ok, or error/kicked/end => failure with the reason), or a timeout.
    return new Promise((resolve) => {
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        connectResolver = null;
        retryConnectBot = null;
        clearTimeout(timer);
        resolve(result);
      };
      const timer = setTimeout(() => {
        finish({
          ok: false,
          detail: lastError || `timed out after ${CONNECT_TIMEOUT_MS} ms waiting to enter the world`,
        });
      }, CONNECT_TIMEOUT_MS);
      // (Re)create the mineflayer bot for the current attempt. Called once up
      // front and again by the 'end' handler when the server dropped us before
      // spawn. Reuses the same finish()/timer so the budget spans all retries.
      const spawnAttempt = () => {
        spawnedThisAttempt = false;
        // A previous attempt's presence poller must not leak into this one.
        if (presencePollTimer) {
          clearInterval(presencePollTimer);
          presencePollTimer = null;
        }
        if (bot) {
          try {
            bot.removeAllListeners();
            bot.quit();
          } catch (e) {
            /* ignore */
          }
          bot = null;
        }
        connected = false;
        connectResolver = finish;
        try {
          bot = mineflayer.createBot(botOpts);
          wireBotEvents(bot);
          log(
            `connecting to ${host}:${port} as ${username}${version ? ` (version ${version})` : ' (auto version)'}`
          );
        } catch (err) {
          const msg = String(err && err.message ? err.message : err);
          lastError = msg;
          finish({ ok: false, detail: msg });
        }
      };
      // The 'end' handler calls this (with a small backoff) to retry a
      // pre-spawn drop without failing the whole /connect.
      retryConnectBot = () => {
        if (settled) return;
        setTimeout(() => {
          if (settled) return;
          spawnAttempt();
        }, CONNECT_RETRY_DELAY_MS);
      };
      spawnAttempt();
    });
  } catch (err) {
    return Promise.resolve({ ok: false, detail: String(err && err.message ? err.message : err) });
  }
}

function disconnectBot() {
  // Intentional teardown: stop the post-spawn auto-reconnect from fighting it.
  intentionalDisconnect = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (presencePollTimer) {
    clearInterval(presencePollTimer);
    presencePollTimer = null;
  }
  if (bot) {
    try {
      bot.quit();
    } catch (e) {
      /* ignore */
    }
  }
  bot = null;
  connected = false;
  return { ok: true };
}

// --- Normalized action -> Mineflayer command -------------------------------

async function runAction(action, payload) {
  payload = payload || {};
  if (!bot || !connected) {
    return { ok: false, detail: 'not connected to a world', data: {} };
  }
  switch (action) {
    case 'say': {
      const text = String(payload.text || '').slice(0, 256);
      if (!text) return { ok: false, detail: 'empty text', data: {} };
      bot.chat(text);
      return { ok: true, detail: 'said', data: { text } };
    }
    case 'skin': {
      // Offline-mode bots cannot set their own texture client-side; the skin is
      // applied server-side. We run a chat command against a server skin plugin
      // (e.g. SkinsRestorer). The Python connector builds the final command from
      // the configured template, so here we only forward and send it verbatim.
      const command = String(payload.command || '').slice(0, 256);
      if (!command) return { ok: false, detail: 'empty skin command', data: {} };
      bot.chat(command);
      return { ok: true, detail: 'skin command sent', data: { command } };
    }
    case 'move': {
      // PoC: brief directional walk. direction in forward/back/left/right.
      const direction = String(payload.direction || 'forward');
      const valid = ['forward', 'back', 'left', 'right'];
      const dir = valid.includes(direction) ? direction : 'forward';
      const durationMs = Math.min(Math.max(parseInt(payload.duration_ms || '600', 10) || 600, 100), 5000);
      try {
        bot.setControlState(dir, true);
        await new Promise((r) => setTimeout(r, durationMs));
        bot.setControlState(dir, false);
        return { ok: true, detail: `moved ${dir}`, data: { position: botPosition() } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'look': {
      const yaw = Number(payload.yaw);
      const pitch = Number(payload.pitch);
      if (Number.isNaN(yaw) || Number.isNaN(pitch)) {
        return { ok: false, detail: 'yaw and pitch required (radians)', data: {} };
      }
      try {
        await bot.look(yaw, pitch, true);
        return { ok: true, detail: 'looked', data: { yaw, pitch } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'use': {
      // PoC: activate held item / interact.
      try {
        bot.activateItem();
        setTimeout(() => {
          try {
            bot.deactivateItem();
          } catch (e) {
            /* ignore */
          }
        }, 200);
        return { ok: true, detail: 'used item', data: {} };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'attack': {
      const entity = resolveTargetEntity(payload.target);
      if (!entity) {
        return { ok: false, detail: 'no target to attack', data: {} };
      }
      try {
        bot.attack(entity);
        const name = entity.username || entity.name || 'entity';
        return { ok: true, detail: `attacked ${name}`, data: { target: name } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'follow': {
      if (!pathfinder || !pathfinder.goals || !bot.pathfinder) {
        return { ok: false, detail: 'follow unavailable (pathfinder not loaded)', data: {} };
      }
      const entity = resolveTargetEntity(payload.target);
      if (!entity) {
        return { ok: false, detail: 'nothing to follow', data: {} };
      }
      try {
        const range = Math.min(Math.max(parseInt(payload.range || '3', 10) || 3, 1), 16);
        bot.pathfinder.setGoal(new pathfinder.goals.GoalFollow(entity, range), true);
        const name = entity.username || entity.name || 'entity';
        return { ok: true, detail: `following ${name}`, data: { target: name, range } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'unfollow': {
      try {
        if (bot.pathfinder && typeof bot.pathfinder.setGoal === 'function') {
          bot.pathfinder.setGoal(null);
        }
        return { ok: true, detail: 'stopped following', data: {} };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'respawn': {
      // Come back to life after death. Mineflayer exposes bot.respawn(), which
      // sends the client_command respawn packet; it is a no-op (and can throw)
      // when the bot is already alive, so guard on the death state.
      if (typeof bot.isAlive === 'boolean' && bot.isAlive) {
        return { ok: false, detail: 'already alive', data: { health: bot.health } };
      }
      try {
        bot.respawn();
        return { ok: true, detail: 'respawned', data: { position: botPosition() } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'surface': {
      // Swim straight up to escape water. In Minecraft the jump control makes
      // the body ascend while submerged, so we hold `jump` and poll the block
      // at head height until it is no longer a liquid (or the timeout hits).
      // This is the correct way to emerge in open water — a pathfinder `goto`
      // toward an air coordinate above has no walkable block to stand on and
      // never surfaces the body (it just drowns). Purely structural: reads the
      // head block id, no keyword logic.
      const timeoutMs = Math.min(Math.max(parseInt(payload.timeout_ms || '4000', 10) || 4000, 500), 15000);
      const headIsLiquid = () => {
        try {
          const p = bot.entity && bot.entity.position;
          if (!p) return false;
          const hb = bot.blockAt(p.offset(0, 1, 0));
          const n = hb && hb.name ? String(hb.name) : '';
          return n === 'water' || n === 'flowing_water' || n === 'bubble_column' || n === 'lava' || n === 'flowing_lava';
        } catch (_e) {
          return false;
        }
      };
      try {
        bot.setControlState('jump', true);
        const deadline = Date.now() + timeoutMs;
        // Poll every 200ms; stop as soon as the head clears the liquid.
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 200));
          if (!headIsLiquid()) break;
        }
        bot.setControlState('jump', false);
        const surfaced = !headIsLiquid();
        return {
          ok: true,
          detail: surfaced ? 'surfaced' : 'surfacing (still submerged)',
          data: { position: botPosition(), surfaced },
        };
      } catch (err) {
        try { bot.setControlState('jump', false); } catch (_e) { /* ignore */ }
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'status': {
      // Full self + surroundings snapshot: health/food/position/time plus the
      // nearby entities, blocks and inventory Synth needs to decide how to act.
      return { ok: true, detail: 'status', data: worldSnapshot(payload) };
    }
    case 'scan': {
      // Explicit "look around" survey. Same snapshot as status but the caller
      // can tune the sampling radius and counts. Kept read-only (no movement).
      const snapshot = worldSnapshot({
        radius: payload.radius,
        max_entities: payload.max_entities,
        max_blocks: payload.max_blocks,
      });
      return {
        ok: true,
        detail: `scanned ${snapshot.entities.length} entities, ${snapshot.blocks.length} block types`,
        data: snapshot,
      };
    }
    case 'goto': {
      // Pathfind to an explicit coordinate, or toward the nearest block whose
      // name matches 'target'. Coordinates win over target when both given.
      // A horizontal-only target (x/z, no y) is valid: it is how autonomous
      // travel steers toward a place on the map without caring about altitude.
      const hasXZ = payload.x != null && payload.z != null;
      if (hasXZ) {
        const x = Number(payload.x);
        const z = Number(payload.z);
        const yGiven = payload.y != null;
        const y = yGiven ? Number(payload.y) : Math.floor(bot.entity.position.y);
        if ([x, z, y].some((n) => Number.isNaN(n))) {
          return { ok: false, detail: 'x, y, z must be numbers', data: {} };
        }
        // A coordinate goto is keyed by its rounded destination so the
        // autonomy motor tick can re-issue it every few seconds without ever
        // interrupting the in-progress walk (see navigateToGoal idempotency).
        const navKey = `xyz:${Math.round(x)}:${Math.round(z)}:${yGiven ? Math.round(y) : 'g'}`;
        // No explicit y → GoalNearXZ steers on the horizontal plane and lets
        // the pathfinder pick the ground height as it travels.
        if (!yGiven && pathfinder.goals.GoalNearXZ) {
          const rangeXZ = Math.min(
            Math.max(parseInt(payload.range || '2', 10) || 2, 0),
            8
          );
          return await navigateToGoal(
            new pathfinder.goals.GoalNearXZ(x, z, rangeXZ),
            payload.timeout_ms,
            navKey
          );
        }
        const range = Math.min(Math.max(parseInt(payload.range || '1', 10) || 1, 0), 8);
        const goal =
          range > 0
            ? new pathfinder.goals.GoalNear(x, y, z, range)
            : new pathfinder.goals.GoalBlock(x, y, z);
        return await navigateToGoal(goal, payload.timeout_ms, navKey);
      }
      const block = resolveTargetBlock(payload.target, payload.search_radius);
      if (!block) {
        return { ok: false, detail: 'no destination (need x/y/z or a reachable target block)', data: {} };
      }
      const p = block.position;
      const range = Math.min(Math.max(parseInt(payload.range || '2', 10) || 2, 0), 8);
      const goal = new pathfinder.goals.GoalNear(p.x, p.y, p.z, range);
      return await navigateToGoal(goal, payload.timeout_ms);
    }
    case 'mine': {
      // Dig the nearest block whose name matches 'target', walking to it first
      // when out of reach, and — crucially — pick up the resulting drop so the
      // item actually lands in the inventory. When mineflayer-collectblock is
      // available it composes navigate → dig → pickup in one verified call;
      // otherwise we fall back to a raw dig. Either way we measure the
      // inventory delta so the caller learns whether the gather truly yielded
      // an item (raw dig on a mis-timed block, creative mode, or a lost drop
      // can "succeed" while collecting nothing).
      const block = resolveTargetBlock(payload.target, payload.search_radius);
      if (!block) {
        return { ok: false, detail: 'no matching block to mine nearby', data: {} };
      }
      try {
        const blockName = block.name;
        // Anticipate the drop id so we can measure the right inventory delta.
        // collectBlock also picks up the drop, so counting the block id alone
        // would miss cases where the drop differs (e.g. stone → cobblestone).
        // We snapshot the whole inventory before and diff after.
        const before = inventoryTotals();
        let usedCollect = false;
        if (
          collectBlock &&
          bot.collectBlock &&
          typeof bot.collectBlock.collect === 'function'
        ) {
          usedCollect = true;
          await bot.collectBlock.collect(block, { ignoreNoPath: false });
        } else {
          const reach = bot.entity.position.distanceTo(block.position);
          if (reach > 4 && pathfinder && bot.pathfinder) {
            const p = block.position;
            const nav = await navigateToGoal(
              new pathfinder.goals.GoalNear(p.x, p.y, p.z, 2),
              payload.timeout_ms,
            );
            if (!nav.ok) return nav;
          }
          if (
            typeof bot.tool === 'object' &&
            bot.tool &&
            typeof bot.tool.equipForBlock === 'function'
          ) {
            try {
              await bot.tool.equipForBlock(block, {});
            } catch (e) {
              /* best-effort tool select */
            }
          }
          if (typeof bot.canDigBlock === 'function' && !bot.canDigBlock(block)) {
            return { ok: false, detail: `cannot dig ${blockName}`, data: {} };
          }
          await bot.dig(block);
          // Give the physics/pickup a brief moment so the drop delta is visible.
          await sleep(400);
        }
        const gained = inventoryDelta(before, inventoryTotals());
        const collected = Object.values(gained).reduce((a, b) => a + b, 0);
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'gather',
          summary:
            collected > 0
              ? `Collected ${collected} from ${blockName}`
              : `Mined ${blockName} (no drop collected)`,
          actor: bot.username,
          data: { block: blockName, collected, gained, position: roundVec(block.position) },
        });
        return {
          ok: true,
          detail:
            collected > 0
              ? `collected ${collected} from ${blockName}`
              : `mined ${blockName} but collected nothing`,
          data: { block: blockName, collected, gained, used_collectblock: usedCollect },
        };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'collect_block': {
      // Gather a specific block id up to `count` times, verifying the inventory
      // grows each round. Structural: `name` is the exact game block id (as
      // reported by scan), never human text. Stops when the target count is
      // reached, no matching block remains nearby, the deadline passes, or a
      // round collects nothing (avoids spinning on an unreachable/undroppable
      // block). Requires collectblock for reliable pickup; falls back to the
      // same dig+delta logic as 'mine' per round when it is unavailable.
      const name = String(payload.name || payload.target || '').trim().toLowerCase();
      if (!name) return { ok: false, detail: 'block name required', data: {} };
      const wantCount = Math.min(Math.max(parseInt(payload.count || '1', 10) || 1, 1), 64);
      const searchRadius = payload.search_radius;
      const deadline =
        Date.now() +
        Math.min(Math.max(parseInt(payload.timeout_ms || '60000', 10) || 60000, 1000), 300000);
      const startTotals = inventoryTotals();
      let rounds = 0;
      let lastDetail = '';
      while (Date.now() < deadline) {
        const collectedSoFar = Object.values(
          inventoryDelta(startTotals, inventoryTotals()),
        ).reduce((a, b) => a + b, 0);
        if (collectedSoFar >= wantCount) break;
        const block = resolveTargetBlock(name, searchRadius);
        if (!block) {
          lastDetail = rounds === 0 ? `no ${name} block nearby` : `no more ${name} nearby`;
          break;
        }
        const before = inventoryTotals();
        try {
          if (
            collectBlock &&
            bot.collectBlock &&
            typeof bot.collectBlock.collect === 'function'
          ) {
            await bot.collectBlock.collect(block, { ignoreNoPath: false });
          } else {
            const reach = bot.entity.position.distanceTo(block.position);
            if (reach > 4 && pathfinder && bot.pathfinder) {
              const p = block.position;
              const nav = await navigateToGoal(
                new pathfinder.goals.GoalNear(p.x, p.y, p.z, 2),
                Math.max(deadline - Date.now(), 1000),
              );
              if (!nav.ok) {
                lastDetail = nav.detail || 'could not reach block';
                break;
              }
            }
            if (
              typeof bot.tool === 'object' &&
              bot.tool &&
              typeof bot.tool.equipForBlock === 'function'
            ) {
              try {
                await bot.tool.equipForBlock(block, {});
              } catch (e) {
                /* best-effort */
              }
            }
            if (typeof bot.canDigBlock === 'function' && !bot.canDigBlock(block)) {
              lastDetail = `cannot dig ${block.name}`;
              break;
            }
            await bot.dig(block);
            await sleep(400);
          }
        } catch (err) {
          lastDetail = String(err && err.message ? err.message : err);
          break;
        }
        const roundGain = Object.values(
          inventoryDelta(before, inventoryTotals()),
        ).reduce((a, b) => a + b, 0);
        rounds += 1;
        if (roundGain <= 0) {
          // Nothing landed this round (creative, lost drop, undroppable) — stop
          // rather than loop forever on the same block.
          lastDetail = `mined ${block.name} but collected nothing`;
          break;
        }
      }
      const gained = inventoryDelta(startTotals, inventoryTotals());
      const collected = Object.values(gained).reduce((a, b) => a + b, 0);
      pushEvent({
        environment: ENVIRONMENT,
        event_type: 'gather',
        summary: `Collected ${collected}/${wantCount} ${name}`,
        actor: bot.username,
        data: { block: name, requested: wantCount, collected, gained, rounds },
      });
      return {
        ok: collected > 0,
        detail:
          collected > 0
            ? `collected ${collected}/${wantCount} ${name}` +
              (lastDetail ? ` (${lastDetail})` : '')
            : lastDetail || `collected nothing`,
        data: { block: name, requested: wantCount, collected, gained, rounds },
      };
    }
    case 'place': {
      // Place a held item named 'item' against the nearest solid block face.
      const itemName = String(payload.item || '').trim().toLowerCase();
      if (!itemName) return { ok: false, detail: 'item name required', data: {} };
      const held = botInventory().find((it) => it.name.toLowerCase() === itemName);
      if (!held) {
        return { ok: false, detail: `not holding any '${itemName}'`, data: {} };
      }
      try {
        const item = bot.inventory.items().find((it) => it.slot === held.slot) || null;
        if (item) await bot.equip(item, 'hand');
        const Vec3 = bot.entity.position.constructor;
        // Find a solid reference block whose exposed air-face yields a target
        // cell the bot does NOT occupy. Placing on the block directly below the
        // bot fails because the target cell overlaps the bot's own body, so the
        // blockUpdate never fires. We scan reference candidates around the feet
        // and pick the first (refBlock, faceVector) whose resulting cell is air
        // and clear of the bot's two body cells.
        const feet = bot.entity.position.floored();
        const bodyCells = [feet, feet.offset(0, 1, 0)];
        const isBodyCell = (p) =>
          bodyCells.some((b) => b.x === p.x && b.y === p.y && b.z === p.z);
        const faces = [
          new Vec3(0, 1, 0),
          new Vec3(1, 0, 0),
          new Vec3(-1, 0, 0),
          new Vec3(0, 0, 1),
          new Vec3(0, 0, -1),
        ];
        // Reference-block candidates: the block under each cell around the bot.
        const around = [
          feet.offset(0, -1, 0),
          feet.offset(1, -1, 0),
          feet.offset(-1, -1, 0),
          feet.offset(0, -1, 1),
          feet.offset(0, -1, -1),
          feet.offset(1, 0, 0),
          feet.offset(-1, 0, 0),
          feet.offset(0, 0, 1),
          feet.offset(0, 0, -1),
        ];
        let chosenRef = null;
        let chosenFace = null;
        for (const refPos of around) {
          const refBlock = bot.blockAt(refPos);
          if (!refBlock || refBlock.name === 'air') continue;
          for (const face of faces) {
            const targetPos = refPos.offset(face.x, face.y, face.z);
            if (isBodyCell(targetPos)) continue;
            const targetBlock = bot.blockAt(targetPos);
            if (targetBlock && targetBlock.name === 'air') {
              chosenRef = refBlock;
              chosenFace = face;
              break;
            }
          }
          if (chosenRef) break;
        }
        if (!chosenRef) {
          return { ok: false, detail: 'no clear face to place against', data: {} };
        }
        // Look at the target cell so the placement raycast lands correctly.
        const lookAt = chosenRef.position
          .offset(0.5, 0.5, 0.5)
          .offset(chosenFace.x * 0.5, chosenFace.y * 0.5, chosenFace.z * 0.5);
        try {
          await bot.lookAt(lookAt, true);
        } catch (_lookErr) {
          // Non-fatal: proceed with placement even if the look call fails.
        }
        await bot.placeBlock(chosenRef, chosenFace);
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'build',
          summary: `Placed ${itemName}`,
          actor: bot.username,
          data: { item: itemName },
        });
        return { ok: true, detail: `placed ${itemName}`, data: { item: itemName } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'inventory': {
      // Read-only: what the bot is carrying.
      const items = botInventory();
      return {
        ok: true,
        detail: `${items.length} item stack(s)`,
        data: { inventory: items },
      };
    }
    case 'wander': {
      // Roam to a random reachable point — free, self-directed exploration.
      // Legs are deliberately long and randomised (TODO: paths were "troppo
      // corti e segmentati"): default radius 48 (~3x the old 16) and a minimum
      // leg of half the radius, so a wander is a real trek rather than a couple
      // of blocks. It stays interruptible: the motor tick re-decides every
      // interval, so an en-route sighting or a benign affordance in reach can
      // redirect before arrival.
      //
      // Directional persistence: the bot keeps a broadly consistent heading
      // across legs instead of picking a fresh random angle each time (which
      // made it "si muove circolarmente" — wander back and forth). We seed a
      // random bearing once, then each leg only drifts it by a small amount so
      // the bot commits to a direction and treks that way. A caller may force a
      // heading via payload.heading (radians) — e.g. to turn around when there
      // *is* a reason to; otherwise the persistent heading is used and updated.
      if (!pathfinder || !bot.pathfinder) {
        return { ok: false, detail: 'wander unavailable (pathfinder not loaded)', data: {} };
      }
      const radius = Math.min(Math.max(parseInt(payload.radius || '48', 10) || 48, 8), 128);
      // Small per-leg drift (±~26°) keeps the path natural without reversing it.
      const MAX_DRIFT = Math.PI / 7;
      let heading;
      if (payload.heading !== undefined && payload.heading !== null && payload.heading !== '') {
        // Explicit heading requested (a real reason to change direction).
        heading = parseFloat(payload.heading);
        if (!Number.isFinite(heading)) heading = null;
      }
      if (heading === null || heading === undefined) {
        if (wanderHeading === null) {
          // First leg of exploration: pick an initial bearing at random.
          wanderHeading = Math.random() * Math.PI * 2;
        } else {
          // Continue broadly the same way, drifting only a little.
          wanderHeading += (Math.random() * 2 - 1) * MAX_DRIFT;
        }
        heading = wanderHeading;
      } else {
        // Adopt the forced heading as the new persistent bearing.
        wanderHeading = heading;
      }
      const minLeg = radius / 2;
      const dist = minLeg + Math.random() * (radius - minLeg);
      const p = bot.entity.position;
      const tx = Math.floor(p.x + Math.cos(heading) * dist);
      const tz = Math.floor(p.z + Math.sin(heading) * dist);
      const goal = new pathfinder.goals.GoalNear(tx, Math.floor(p.y), tz, 2);
      const nav = await navigateToGoal(goal, payload.timeout_ms);
      if (nav.ok) {
        nav.detail = 'wandered';
      } else {
        // Blocked this way — rotate the heading substantially so the next leg
        // tries a genuinely different direction instead of hammering the wall.
        wanderHeading = (wanderHeading + Math.PI / 2 + Math.random() * (Math.PI / 2)) % (Math.PI * 2);
      }
      return nav;
    }
    case 'craft': {
      // Craft an item by its resolved item name (e.g. "oak_planks", "stick",
      // "crafting_table"). Purely structural: the caller supplies the exact
      // Minecraft item name; the bridge never interprets what a name means.
      // Recipes needing a 3x3 grid require a crafting table — we auto-locate
      // the nearest one and walk to it when out of reach.
      const itemName = String(payload.item || '').trim().toLowerCase();
      if (!itemName) return { ok: false, detail: 'item name required', data: {} };
      const count = Math.min(Math.max(parseInt(payload.count || '1', 10) || 1, 1), 64);
      const version = bot.version;
      const mcData = minecraftData && version ? minecraftData(version) : null;
      if (!mcData || !mcData.itemsByName) {
        return { ok: false, detail: 'crafting unavailable (minecraft-data not loaded)', data: {} };
      }
      const itemDef = mcData.itemsByName[itemName];
      if (!itemDef) {
        return { ok: false, detail: `unknown item '${itemName}'`, data: {} };
      }
      try {
        // Locate a nearby crafting table (needed for 3x3 recipes). When none is
        // reachable, we still attempt inventory-grid (2x2) recipes below.
        let craftingTable = null;
        const tableBlock = resolveTargetBlock('crafting_table', payload.search_radius);
        if (tableBlock) {
          const reach = bot.entity.position.distanceTo(tableBlock.position);
          if (reach > 3.5 && pathfinder && bot.pathfinder) {
            const p = tableBlock.position;
            await navigateToGoal(new pathfinder.goals.GoalNear(p.x, p.y, p.z, 2), payload.timeout_ms);
          }
          craftingTable = tableBlock;
        }
        // recipesFor(itemId, metadata, minResultCount, craftingTable) returns
        // only recipes the bot can actually make with what it is holding.
        let recipes = bot.recipesFor(itemDef.id, null, 1, craftingTable);
        if ((!recipes || !recipes.length) && !craftingTable) {
          // Retry without a table constraint in case a 2x2 recipe exists.
          recipes = bot.recipesFor(itemDef.id, null, 1, null);
        }
        if (!recipes || !recipes.length) {
          const needsTable = craftingTable
            ? ''
            : ' (a crafting table may be required and none is reachable)';
          return {
            ok: false,
            detail: `no craftable recipe for '${itemName}' with current materials${needsTable}`,
            data: {},
          };
        }
        await bot.craft(recipes[0], count, craftingTable || null);
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'craft',
          summary: `Crafted ${count}x ${itemName}`,
          actor: bot.username,
          data: { item: itemName, count, used_table: Boolean(craftingTable) },
        });
        return {
          ok: true,
          detail: `crafted ${count}x ${itemName}`,
          data: { item: itemName, count, used_table: Boolean(craftingTable) },
        };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'smelt': {
      // Smelt an input item in a nearby furnace (e.g. raw_iron -> iron_ingot).
      // Purely structural: the caller supplies the exact input (and optional
      // fuel) item names; the bridge never interprets what a name means. Auto-
      // locates the nearest furnace and walks to it when out of reach.
      const inputName = String(payload.item || payload.input || '').trim().toLowerCase();
      if (!inputName) return { ok: false, detail: 'input item name required', data: {} };
      const count = Math.min(Math.max(parseInt(payload.count || '1', 10) || 1, 1), 64);
      const fuelName = String(payload.fuel || 'coal').trim().toLowerCase();
      const version = bot.version;
      const mcData = minecraftData && version ? minecraftData(version) : null;
      if (!mcData || !mcData.itemsByName) {
        return { ok: false, detail: 'smelting unavailable (minecraft-data not loaded)', data: {} };
      }
      const inputDef = mcData.itemsByName[inputName];
      if (!inputDef) return { ok: false, detail: `unknown item '${inputName}'`, data: {} };
      // A furnace or blast_furnace is required and must be reachable.
      let furnaceBlock =
        resolveTargetBlock('furnace', payload.search_radius) ||
        resolveTargetBlock('blast_furnace', payload.search_radius);
      if (!furnaceBlock) {
        return { ok: false, detail: 'no reachable furnace nearby', data: {} };
      }
      try {
        if (
          bot.entity.position.distanceTo(furnaceBlock.position) > 3.5 &&
          pathfinder &&
          bot.pathfinder
        ) {
          const p = furnaceBlock.position;
          await navigateToGoal(new pathfinder.goals.GoalNear(p.x, p.y, p.z, 2), payload.timeout_ms);
        }
        const furnace = await bot.openFurnace(furnaceBlock);
        try {
          const fuelDef = mcData.itemsByName[fuelName];
          if (fuelDef) {
            const haveFuel = bot.inventory
              .items()
              .some((it) => it.type === fuelDef.id);
            if (haveFuel) {
              try {
                await furnace.putFuel(fuelDef.id, null, 1);
              } catch (fuelErr) {
                /* fuel may already be present; continue */
              }
            }
          }
          await furnace.putInput(inputDef.id, null, count);
        } finally {
          // Give the furnace a moment; the caller re-checks via inventory on a
          // later beat (progress is Synth's own judgement, not counted here).
          try {
            furnace.close();
          } catch (e) {
            /* ignore */
          }
        }
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'smelt',
          summary: `Smelting ${count}x ${inputName}`,
          actor: bot.username,
          data: { item: inputName, count, fuel: fuelName },
        });
        return {
          ok: true,
          detail: `smelting ${count}x ${inputName}`,
          data: { item: inputName, count, fuel: fuelName },
        };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    case 'equip': {
      // Wear/hold an item the bot is carrying (e.g. iron_helmet, iron_chestplate,
      // a shield or a tool). 'destination' defaults to the correct armor slot
      // inferred by mineflayer; pass an explicit 'slot' to override
      // (head/torso/legs/feet/hand/off-hand). Purely structural.
      const itemName = String(payload.item || '').trim().toLowerCase();
      if (!itemName) return { ok: false, detail: 'item name required', data: {} };
      try {
        const item = bot.inventory
          .items()
          .find((it) => it.name && it.name.toLowerCase() === itemName);
        if (!item) {
          return { ok: false, detail: `not carrying '${itemName}'`, data: {} };
        }
        const rawSlot = String(payload.slot || '').trim().toLowerCase();
        const allowedSlots = ['head', 'torso', 'legs', 'feet', 'hand', 'off-hand'];
        let dest = allowedSlots.includes(rawSlot) ? rawSlot : null;
        if (!dest) {
          // Infer the armor slot from the item name suffix; fall back to hand.
          if (itemName.endsWith('helmet')) dest = 'head';
          else if (itemName.endsWith('chestplate')) dest = 'torso';
          else if (itemName.endsWith('leggings')) dest = 'legs';
          else if (itemName.endsWith('boots')) dest = 'feet';
          else if (itemName === 'shield') dest = 'off-hand';
          else dest = 'hand';
        }
        await bot.equip(item, dest);
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'equip',
          summary: `Equipped ${itemName} (${dest})`,
          actor: bot.username,
          data: { item: itemName, slot: dest },
        });
        return { ok: true, detail: `equipped ${itemName}`, data: { item: itemName, slot: dest } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
    }
    default:
      return { ok: false, detail: `unknown action: ${action}`, data: {} };
  }
}

// --- HTTP server -----------------------------------------------------------

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 1_000_000) {
        // 1 MB guard
        data = data.slice(0, 1_000_000);
      }
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch (e) {
        resolve({});
      }
    });
    req.on('error', () => resolve({}));
  });
}

const server = http.createServer(async (req, res) => {
  const url = req.url || '/';
  try {
    if (req.method === 'GET' && url.startsWith('/health')) {
      return sendJson(res, 200, {
        ok: true,
        connected,
        username: bot ? bot.username || CFG.username : CFG.username,
        environment: ENVIRONMENT,
        mineflayer: !!mineflayer,
        last_error: lastError,
      });
    }
    if (req.method === 'GET' && url.startsWith('/events')) {
      return sendJson(res, 200, { events: drainEvents() });
    }
    if (req.method === 'POST' && url.startsWith('/cmd')) {
      const body = await readBody(req);
      const result = await runAction(String(body.action || ''), body.payload || {});
      return sendJson(res, 200, result);
    }
    if (req.method === 'POST' && url.startsWith('/connect')) {
      const body = await readBody(req);
      const result = await connectBot(body || {});
      return sendJson(res, result.ok ? 200 : 500, result);
    }
    if (req.method === 'POST' && url.startsWith('/disconnect')) {
      return sendJson(res, 200, disconnectBot());
    }
    return sendJson(res, 404, { ok: false, detail: 'not found' });
  } catch (err) {
    return sendJson(res, 500, { ok: false, detail: String(err && err.message ? err.message : err) });
  }
});

// A stale bridge that lost its HTTP server but whose Node process is still
// alive (e.g. a lingering mineflayer TCP socket keeping the event loop up)
// would make a fresh bridge crash here with an UNHANDLED 'error' event
// (`throw er`). Handle EADDRINUSE gracefully: log it and exit cleanly so the
// provisioner's reaper can free the port and the next launch can succeed —
// never crash with a stack trace.
server.on('error', (err) => {
  const code = err && err.code ? err.code : '';
  if (code === 'EADDRINUSE') {
    log(`port ${CFG.bridgeHost}:${CFG.bridgePort} already in use — exiting so it can be reclaimed`);
  } else {
    log(`http server error: ${String(err && err.message ? err.message : err)}`);
  }
  // Exit cleanly; do not let an unhandled 'error' event crash the process.
  try {
    process.exit(1);
  } catch (_e) {
    /* ignore */
  }
});

server.listen(CFG.bridgePort, CFG.bridgeHost, () => {
  log(`listening on http://${CFG.bridgeHost}:${CFG.bridgePort}`);
  log(`target server ${CFG.serverHost}:${CFG.serverPort} as ${CFG.username} (${CFG.auth})`);
});

let shuttingDown = false;

function shutdown() {
  // Guard against re-entrancy (double signal) — otherwise a second SIGTERM
  // could race the exit and leave the process hanging.
  if (shuttingDown) return;
  shuttingDown = true;
  log('shutting down');
  try {
    disconnectBot();
  } catch (e) {
    /* ignore */
  }
  // bot.quit() may leave a lingering TCP socket / plugin timer that keeps the
  // Node event loop alive, turning the process into a "zombie" whose HTTP
  // server is closed (connection refused) but whose PID never exits. Force a
  // hard exit shortly after closing the server so the process ALWAYS dies and
  // the port is released for the next launch. The fallback timer is unref'd so
  // it can never itself keep the loop alive.
  try {
    server.close(() => process.exit(0));
  } catch (_e) {
    process.exit(0);
  }
  const hardExit = setTimeout(() => process.exit(0), 1000);
  if (typeof hardExit.unref === 'function') hardExit.unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
