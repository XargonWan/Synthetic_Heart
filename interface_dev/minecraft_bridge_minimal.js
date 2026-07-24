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
 *                                       follow | unfollow | status | skin
 *                              (skin payload: { command } — a server skin-plugin
 *                              chat command built by the Python connector)
 *                              (follow requires mineflayer-pathfinder; when it
 *                              is not installed the action fails gracefully)
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
 * This file lives in interface_dev/ because it is a developer/runtime helper,
 * not part of the Python import graph.
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

// Optional: pathfinder enables entity-following. If the module is not
// installed the bridge still works — 'follow' just fails with a clear message.
let pathfinder = null;
try {
  // eslint-disable-next-line global-require
  pathfinder = require('mineflayer-pathfinder');
} catch (err) {
  pathfinder = null;
}

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

function wireBotEvents(b) {
  b.on('spawn', () => {
    // 'spawn' is the authoritative "we are actually in the world" signal.
    connected = true;
    lastError = null;
    settleConnect({ ok: true, detail: 'spawned' });
  });

  b.on('login', () => {
    connected = true;
    log('logged in as', b.username);
    if (pathfinder && pathfinder.pathfinder) {
      try {
        b.loadPlugin(pathfinder.pathfinder);
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
    if (username === b.username) return;
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'chat',
      summary: `${username}: ${message}`,
      actor: username,
      data: { message },
    });
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

  b.on('entityHurt', (entity) => {
    if (!bot || !entity || entity !== bot.entity) return;
    pushEvent({
      environment: ENVIRONMENT,
      event_type: 'damage',
      summary: 'Took damage',
      actor: b.username,
      data: { health: bot.health },
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
      return {
        ok: true,
        detail: 'status',
        data: {
          connected,
          username: bot.username || CFG.username,
          health: typeof bot.health === 'number' ? bot.health : null,
          position: botPosition(),
        },
      };
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
