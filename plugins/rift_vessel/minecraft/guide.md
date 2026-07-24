# Minecraft Vessel connector

Lets Synth embody a bot in a Minecraft world. It is a connector for the Rift
Vessel subsystem: it speaks plain HTTP to a local Node.js **Mineflayer bridge**
and maps normalized Vessel actions
(`say` / `move` / `look` / `use` / `attack` / `follow` / `unfollow` / `respawn` /
`status` / `skin`) to bridge commands, while world events are polled and forwarded
as normalized `PerceptionEvent`s. `follow` requires the optional
`mineflayer-pathfinder` package; without it the action fails gracefully.
`respawn` calls Mineflayer `bot.respawn()` to come back to life after dying and
is guarded to no-op when the bot is already alive.

The connector never creates agentic tasks and never writes diary/memory — the
interface buffers experiences and flushes a single entry at end-of-session.

## Enabling

1. **Enable the plugin** — flip the Minecraft Vessel plugin **on** from its card
   in the WebUI Plugins tab. The bridge provisioner is gated by this toggle
   (`PLUGIN_ENABLED__minecraft_vessel`); there is no separate bridge master switch.
   Note: if this plugin is enabled but the **Rift Vessel** core plugin is
   disabled, the card shows an **orange** LED — Minecraft can't connect until the
   core Vessel plugin is also enabled.
2. **Select the connector** — set `ACTIVE_VESSEL = minecraft`. Because Vessel
   actions are **connection-driven**, while disconnected Synth only sees a single
   `vessel_connect` action whose `game` enum includes `minecraft`. Once it
   connects, the embodiment verbs appear namespaced as `vessel_minecraft_<verb>`
   (e.g. `vessel_minecraft_say`) and `vessel_connect` disappears until logout.
3. **Point it at your server** — set `MINECRAFT_SERVER_HOST` / `MINECRAFT_SERVER_PORT`
   (and, optionally, the other keys in the table below). These are the
   **defaults**; Synth can also join a different server on request — the
   `vessel_connect` action accepts optional `host` / `port` fields that
   override the configured address for that connect only.

   > **Hosting the world on the same machine as SyntH?** Just leave
   > `MINECRAFT_SERVER_HOST` at `127.0.0.1` (the default) and open your world to
   > LAN. When SyntH runs in Docker, a loopback host is **automatically
   > remapped** to `host.docker.internal` (the Docker host), so the bot reaches
   > your world with no manual network setup. This relies on the
   > `extra_hosts: ["host.docker.internal:host-gateway"]` entry shipped in
   > `docker-compose.yml`. A real LAN / remote / VPN address is never remapped.
   > If your server lives on a **different** machine, point
   > `MINECRAFT_SERVER_HOST` at an address that machine is actually reachable
   > on (LAN IP or VPN IP) — no software can reach an unroutable host.

## Node.js requirement

The Node.js **Mineflayer bridge** needs Node to run.

- **Docker (default deployment):** Node is **baked into the image** — the
  Minecraft Vessel works out of the box, no extra build flags needed.
- **Non-Docker / bare-metal:** install Node.js (LTS, v22+) yourself. To build a
  deliberately node-free Docker image (e.g. when you don't need the Vessel),
  opt out with `docker build --build-arg INSTALL_NODE=false …`.

The provisioner runs the bridge as a **non-root** subprocess and returns a
clear error if `node` / `npm` are missing.

## Provisioning commands (CLI)

```
/minecraft provision start
/minecraft provision stop
/minecraft provision status
/minecraft provision logs [n]
```

## Config keys

### Global Vessel settings (core `vessel_plugin`)

These live on the Rift Vessel **core** and apply to every world, not just Minecraft.

| Key | Default | Purpose |
|-----|---------|---------|
| `ACTIVE_VESSEL` | `disabled` | Which world connector is active. Set to `minecraft` to use this connector. |
| `VESSEL_SESSION_COOLDOWN_SEC` | `3600` | Seconds of in-world inactivity before a session is auto-closed (triggers the single end-of-session diary entry). |
| `VESSEL_SETTINGS` | *(empty)* | Optional JSON blob for per-world extra settings. |

### Minecraft connector settings (`minecraft_vessel`)

The bridge itself is gated by the plugin's own enable toggle
(`PLUGIN_ENABLED__minecraft_vessel`) — there is no separate `MINECRAFT_BRIDGE_ENABLED`
key. Enable/disable the whole thing from the plugin card.

| Key | Default | Advanced | Purpose |
|-----|---------|----------|---------|
| `MINECRAFT_BRIDGE_RUN_AT_START` | `False` | ✓ | Optional: start the bridge at boot. By default the bridge starts **on demand**, only when Synth actually enters the world. |
| `MINECRAFT_BRIDGE_HOST` | `127.0.0.1` | ✓ | Bridge HTTP host |
| `MINECRAFT_BRIDGE_PORT` | `8137` | ✓ | Bridge HTTP port |
| `MINECRAFT_SERVER_HOST` | `127.0.0.1` | | Minecraft server host |
| `MINECRAFT_SERVER_PORT` | `44383` | | Minecraft server port |
| `MINECRAFT_SERVER_VERSION` | *(empty)* | ✓ | Optional protocol version to pin (e.g. `1.21.4`). Leave empty to auto-detect. Set it if the bridge log shows `No data available for version X` — see **Troubleshooting** below. **Maximum supported: `1.21.11`** (see below). |
| `MINECRAFT_BOT_USERNAME_OVERRIDE` | *(empty)* | ✓ | Optional in-world bot username. Leave empty to use Synth's configured name (`SYNTH_NAME`). |
| `MINECRAFT_SKIN_FILE` | *(empty)* | | Uploaded skin texture PNG (file upload in the plugin card). Served over HTTP and applied at spawn. Requires a server skin plugin. |
| `MINECRAFT_SKIN_MODEL` | `classic` | | Model variant (dropdown): `classic` (Steve) or `slim` (Alex). |
| `MINECRAFT_SKIN_PUBLIC_BASE_URL` | *(empty)* | ✓ | Public base URL the Minecraft server can reach to fetch the uploaded skin. Leave empty to auto-derive: SyntH uses the WebUI host, and if that is a loopback it substitutes the machine's primary LAN IP so a server on the same LAN can reach it. Set explicitly for a VPN/public/reverse-proxy address. |
| `MINECRAFT_SKIN_COMMAND_TEMPLATE` | `/skin url {url}` | ✓ | Chat command run at spawn. `{url}` / `{model}` are substituted. |

## Skin — how it works (important)

**A real client-side skin upload is not possible for an offline-mode bot.** In
offline auth the skin is not carried by the client — it is determined by the
server (by username/UUID or a skin-management plugin). Mineflayer exposes only
read-only skin data and cape/sleeve *visibility* toggles, not the texture.

The **only** functional way to give Synth's bot a custom skin on an offline
server is a **server-side skin plugin** such as
[SkinsRestorer](https://skinsrestorer.net/). When one is installed, the
connector applies the skin automatically at spawn.

**Uploading a PNG:** upload the skin texture directly from the plugin card — the
`Minecraft Skin File` field is a file upload. SyntH stores the file and serves
it over HTTP at `<base>/api/config/MINECRAFT_SKIN_FILE/file`. At spawn the
connector runs `MINECRAFT_SKIN_COMMAND_TEMPLATE` (default `/skin url {url}`),
substituting that URL for `{url}` and `MINECRAFT_SKIN_MODEL` for `{model}`.

The `<base>` is `MINECRAFT_SKIN_PUBLIC_BASE_URL` when set; otherwise it is
auto-derived from the WebUI host/port, and when that host is a loopback
(`127.0.0.1`/`localhost`/`0.0.0.0`) SyntH substitutes the machine's primary LAN
IP so a server on the same LAN can reach it out of the box. **The Minecraft
server must be able to reach that URL** to fetch the texture — for a server on a
different network (VPN, public host, behind a reverse proxy) set
`MINECRAFT_SKIN_PUBLIC_BASE_URL` explicitly to a value reachable from the server.

The command template is configurable so any skin plugin (or a non-English
server) can be supported without changing code. If no skin plugin is present the
command is simply ignored by the server and the bot keeps the default skin. If
`MINECRAFT_SKIN_FILE` is empty, no skin command is sent at all.

### The skin doesn't appear — checklist

The connector logs `skin command sent: <command>` at spawn, so the command going
out is **not** proof the skin was applied. Two independent conditions must both
hold on the **server** side:

1. **A server-side skin plugin must be installed.** On an offline-mode server
   the only working path is a plugin such as
   [SkinsRestorer](https://skinsrestorer.net/) (or another that understands a
   `/skin`-style command). Without it the `/skin url …` command is silently
   ignored — there is nothing SyntH can do from the client. If the plugin uses a
   different command syntax, adapt `MINECRAFT_SKIN_COMMAND_TEMPLATE`
   (`{url}` / `{model}` are substituted).

2. **The server must be able to reach the skin URL.** The command carries
   `<base>/api/config/MINECRAFT_SKIN_FILE/file`. When `MINECRAFT_SKIN_PUBLIC_BASE_URL`
   is empty SyntH auto-derives the base from the WebUI host, substituting the
   machine's LAN IP for a loopback host — this covers the common "SyntH host +
   server on the same LAN" case automatically. It is **still unreachable** for a
   server on a different network (a different subnet, a VPN-only peer, a public
   host), so in that case set `MINECRAFT_SKIN_PUBLIC_BASE_URL` to an address the
   server can actually open (the SyntH host's VPN/public IP + the WebUI HTTP
   port), then verify from the server's network with:

   ```
   curl -I http://<synth-host-ip>:<port>/api/config/MINECRAFT_SKIN_FILE/file
   ```

   It must return `200 OK`.

After fixing either, **reconnect the Vessel** (`vessel_disconnect` then
`vessel_connect`) so `_apply_skin` runs again at spawn.

## Bridge lifecycle — on demand

The Mineflayer bridge is **not** started at boot. It is launched automatically
the first time Synth enters the world (when the connector connects), and stays
up for the session. Set `MINECRAFT_BRIDGE_RUN_AT_START = True` only if you want
the bridge running before the first session (e.g. for pre-warming or manual
testing).

## Supported Minecraft versions

The bridge bundles **Mineflayer 4.37.1** (`minecraft-data` 3.111.0,
`minecraft-protocol` 4.x). The **highest Minecraft release it can join is
`1.21.11`** — this is also the bridge's default when no version is pinned.

Supported protocol versions (a Vanilla server on any of these works out of the
box): `1.7`, `1.8.8`, `1.9.4`, `1.10.2`, `1.11.2`, `1.12.2`, `1.13.2`, `1.14.4`,
`1.15.2`, `1.16.5`, `1.17.1`, `1.18.2`, `1.19`–`1.19.4`, `1.20`–`1.20.6`,
`1.21.1`, `1.21.3`–`1.21.6`, `1.21.8`, `1.21.9`, `1.21.11`.

> **A server newer than `1.21.11` will fail** with `No data available for
> version X`, because the bundled `minecraft-data` has no protocol tables for
> it yet. Run your world on `1.21.11` or older, or bump the bridge's Node
> dependencies (see the note at the end of **Troubleshooting**).

## Troubleshooting

### `No data available for version X` in the bridge log

If the bot reaches the server but disconnects with an error like
`No data available for version 26.2` (visible in
`/opt/minecraft_bridge/bridge.log`), the server announced a protocol version
that the bundled Mineflayer data doesn't recognise — this happens with very new
releases, snapshots, or some proxies/mods. Mineflayer's auto-detection then
fails.

**Fix:** set `MINECRAFT_SERVER_VERSION` (advanced setting) to a supported
version string close to your server, e.g. `1.21.4`, and reconnect. This pins the
protocol instead of relying on auto-detection. Leave it empty to restore
auto-detection.

> If your server is genuinely newer than the bridge's bundled data, you may also
> need to bump the `mineflayer` / `minecraft-data` versions in the bridge's
> `package.json` and reinstall.

## Scope

Offline auth only (no Microsoft/XBL). Advanced multiplayer sync is out of scope.

Full reference: `docs/rift_vessel.rst`.
