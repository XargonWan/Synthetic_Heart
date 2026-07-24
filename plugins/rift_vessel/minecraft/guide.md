# Minecraft Vessel connector (PoC)

Lets Synth embody a bot in a Minecraft world. It is a **proof-of-concept**
connector for the Rift Vessel subsystem: it speaks plain HTTP to a local
Node.js **Mineflayer bridge** and maps normalized Vessel actions
(`say` / `move` / `look` / `use` / `status`) to bridge commands, while world
events are polled and forwarded as normalized `PerceptionEvent`s.

The connector never creates agentic tasks and never writes diary/memory — the
interface buffers experiences and flushes a single entry at end-of-session.

## Enabling

1. Select the connector: set `ACTIVE_VESSEL = minecraft`.
2. Enable the bridge: set `MINECRAFT_BRIDGE_ENABLED = True`.
3. Point it at your server via the config keys below.

## Build requirement — Node is opt-in

The default runtime image (`python:3.12-slim`) has **no Node.js**. The bridge
needs it, so build the image with:

```bash
docker build --build-arg INSTALL_NODE=true …
```

The provisioner runs the bridge as a **non-root** subprocess and returns a
clear error if `node`/`npm` are missing.

## Provisioning commands (CLI)

```
/minecraft provision start
/minecraft provision stop
/minecraft provision status
/minecraft provision logs [n]
```

## Config keys

| Key | Default | Purpose |
|-----|---------|---------|
| `MINECRAFT_BRIDGE_ENABLED` | `False` | Master switch for the bridge |
| `MINECRAFT_BRIDGE_RUN_AT_START` | — | Auto-start the bridge on boot |
| `MINECRAFT_BRIDGE_HOST` | `127.0.0.1` | Bridge HTTP host |
| `MINECRAFT_BRIDGE_PORT` | `8137` | Bridge HTTP port |
| `MINECRAFT_SERVER_HOST` | `127.0.0.1` | Minecraft server host |
| `MINECRAFT_SERVER_PORT` | `25565` | Minecraft server port |
| `MINECRAFT_BOT_USERNAME` | `Synth` | In-world bot username |

## Scope

Offline auth only (no Microsoft/XBL). Advanced multiplayer sync is out of scope
for the PoC.

Full reference: `docs/rift_vessel.rst`.
