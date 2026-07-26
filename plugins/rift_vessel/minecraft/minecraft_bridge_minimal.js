#!/usr/bin/env node
/*
 * minecraft_bridge_minimal.js — Rift Vessel Minecraft bridge
 * ---------------------------------------------------------------
 * A minimal Mineflayer <-> HTTP bridge that lets the SyntH Minecraft Vessel
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

function timeOfDay() {
  if (!bot || !bot.time) return null;
  const t = typeof bot.time.timeOfDay === 'number' ? bot.time.timeOfDay : null;
  // Minecraft day = 24000 ticks; 0..12000 is daytime.
  const isDay = t == null ? null : t >= 0 && t < 12000;
  return { time_of_day: t, is_day: isDay };
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
  b.on('spawn', () => {
    // 'spawn' is the authoritative "we are actually in the world" signal.
    connected = true;
    lastError = null;
    // Fresh session → fresh exploration bearing (see the wander case).
    wanderHeading = null;
    settleConnect({ ok: true, detail: 'spawned' });
  });

  b.on('login', () => {
    connected = true;
    log('logged in as', b.username);
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
    lastError = `kicked: ${String(reason)}`;
    settleConnect({ ok: false, detail: lastError });
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'disconnect',
      summary: `Kicked: ${String(reason)}`,
      actor: b.username,
      data: { reason: String(reason) },
    });
  });

  b.on('end', () => {
    connected = false;
    log('connection ended');
    // If the connection ended before we ever spawned, treat it as a failed
    // connect so /connect can report a real reason (fall back to lastError).
    settleConnect({ ok: false, detail: lastError || 'connection ended before spawn' });
  });

  b.on('error', (err) => {
    const msg = err && err.message ? err.message : String(err);
    lastError = msg;
    log('bot error:', msg);
    settleConnect({ ok: false, detail: msg });
  });
}

// How long to wait for the bot to actually reach the world (spawn) or fail
// after createBot before /connect gives up and reports a timeout.
const CONNECT_TIMEOUT_MS = 20000;

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
    };
    if (version) {
      botOpts.version = version;
    }
    // Await the real outcome: resolve on the first terminal event (spawn =>
    // ok, or error/kicked/end => failure with the reason), or a timeout.
    return new Promise((resolve) => {
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        connectResolver = null;
        clearTimeout(timer);
        resolve(result);
      };
      const timer = setTimeout(() => {
        finish({
          ok: false,
          detail: lastError || `timed out after ${CONNECT_TIMEOUT_MS} ms waiting to enter the world`,
        });
      }, CONNECT_TIMEOUT_MS);
      connectResolver = finish;
      try {
        bot = mineflayer.createBot(botOpts);
        wireBotEvents(bot);
        log(`connecting to ${host}:${port} as ${username}${version ? ` (version ${version})` : ' (auto version)'}`);
      } catch (err) {
        const msg = String(err && err.message ? err.message : err);
        lastError = msg;
        finish({ ok: false, detail: msg });
      }
    });
  } catch (err) {
    return Promise.resolve({ ok: false, detail: String(err && err.message ? err.message : err) });
  }
}

function disconnectBot() {
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
      // Dig the nearest block whose name matches 'target'. Auto-equips the best
      // available tool. Walks to the block first when it is out of reach.
      const block = resolveTargetBlock(payload.target, payload.search_radius);
      if (!block) {
        return { ok: false, detail: 'no matching block to mine nearby', data: {} };
      }
      try {
        const reach = bot.entity.position.distanceTo(block.position);
        if (reach > 4 && pathfinder && bot.pathfinder) {
          const p = block.position;
          const nav = await navigateToGoal(new pathfinder.goals.GoalNear(p.x, p.y, p.z, 2), payload.timeout_ms);
          if (!nav.ok) return nav;
        }
        if (typeof bot.tool === 'object' && bot.tool && typeof bot.tool.equipForBlock === 'function') {
          try {
            await bot.tool.equipForBlock(block, {});
          } catch (e) {
            /* best-effort tool select */
          }
        }
        if (typeof bot.canDigBlock === 'function' && !bot.canDigBlock(block)) {
          return { ok: false, detail: `cannot dig ${block.name}`, data: {} };
        }
        await bot.dig(block);
        pushEvent({
          environment: ENVIRONMENT,
          event_type: 'gather',
          summary: `Mined ${block.name}`,
          actor: bot.username,
          data: { block: block.name, position: roundVec(block.position) },
        });
        return { ok: true, detail: `mined ${block.name}`, data: { block: block.name } };
      } catch (err) {
        return { ok: false, detail: String(err && err.message ? err.message : err), data: {} };
      }
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

server.listen(CFG.bridgePort, CFG.bridgeHost, () => {
  log(`listening on http://${CFG.bridgeHost}:${CFG.bridgePort}`);
  log(`target server ${CFG.serverHost}:${CFG.serverPort} as ${CFG.username} (${CFG.auth})`);
});

function shutdown() {
  log('shutting down');
  try {
    disconnectBot();
  } catch (e) {
    /* ignore */
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
