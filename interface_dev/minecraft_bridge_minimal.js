#!/usr/bin/env node
/*
 * minecraft_bridge_minimal.js — Rift Vessel Minecraft PoC bridge
 * ---------------------------------------------------------------
 * A minimal Mineflayer <-> HTTP bridge that lets the SyntH Minecraft Vessel
 * connector drive an in-world bot and receive normalized world events.
 *
 * Design (see docs/rift_vessel.rst):
 *   - The Python connector (plugins/vessels/minecraft_connector.py) talks to
 *     this process over plain HTTP on 127.0.0.1.
 *   - This bridge translates SyntH normalized actions -> Mineflayer commands
 *     (POST /cmd) and Mineflayer events -> normalized perception events, which
 *     it buffers for the connector to pull (POST /events long-poll style GET).
 *   - Offline-mode by default (PoC): no Microsoft/XBL auth.
 *
 * Endpoints:
 *   GET  /health            -> { ok, connected, username, environment }
 *   GET  /events            -> { events: [ {event_type, summary, actor, data} ] }
 *                              (drains and returns the buffered events)
 *   POST /cmd               -> { ok, detail, data }
 *                              body: { action, payload }
 *   POST /connect           -> { ok, detail }   (re)connect to the server
 *   POST /disconnect        -> { ok }
 *
 * Env / args (all optional, sensible PoC defaults):
 *   BRIDGE_HOST         (default 127.0.0.1)
 *   BRIDGE_PORT         (default 8137)
 *   MC_SERVER_HOST      (default 127.0.0.1)
 *   MC_SERVER_PORT      (default 25565)
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

const CFG = {
  bridgeHost: process.env.BRIDGE_HOST || '127.0.0.1',
  bridgePort: parseInt(process.env.BRIDGE_PORT || '8137', 10),
  serverHost: process.env.MC_SERVER_HOST || '127.0.0.1',
  serverPort: parseInt(process.env.MC_SERVER_PORT || '25565', 10),
  username: process.env.MC_BOT_USERNAME || 'Synth',
  auth: process.env.MC_AUTH || 'offline',
};

const ENVIRONMENT = 'minecraft';
const EVENT_BUFFER_MAX = 500;

/** @type {Array<object>} */
let eventBuffer = [];
/** @type {import('mineflayer').Bot | null} */
let bot = null;
let connected = false;

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

function wireBotEvents(b) {
  b.on('login', () => {
    connected = true;
    log('logged in as', b.username);
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
      summary: 'Died in the world',
      actor: b.username,
      data: {},
    });
  });

  b.on('kicked', (reason) => {
    connected = false;
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
  });

  b.on('error', (err) => {
    log('bot error:', err && err.message ? err.message : err);
  });
}

function connectBot() {
  if (!mineflayer) {
    return { ok: false, detail: 'mineflayer module not installed' };
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
  try {
    bot = mineflayer.createBot({
      host: CFG.serverHost,
      port: CFG.serverPort,
      username: CFG.username,
      auth: CFG.auth,
    });
    wireBotEvents(bot);
    return { ok: true, detail: 'connecting' };
  } catch (err) {
    return { ok: false, detail: String(err && err.message ? err.message : err) };
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
      const result = connectBot();
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
