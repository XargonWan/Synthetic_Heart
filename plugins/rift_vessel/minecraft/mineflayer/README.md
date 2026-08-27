# Minecraft bridge — self-contained Node runtime

This folder is part of the **distributable Minecraft Vessel plugin package**.
The whole `plugins/rift_vessel/minecraft/` tree can be shipped (e.g. as a zip)
and dropped into a SyntH install: everything the bridge needs lives inside the
plugin, never in a shared `/opt` path that is lost when the container is
recreated.

## Layout

| Path | Committed? | Purpose |
|------|-----------|---------|
| `package.json` | ✅ source | Pins the bridge's Node dependencies (`mineflayer`, `mineflayer-pathfinder`, `minecraft-data`). Ships with the package. |
| `node_modules/` | ❌ artefact | Installed by `BridgeProvisioner.install()` (or `npm install` here). Gitignored. May be pre-populated inside the shipped zip for a fully offline package. |
| `bridge.json` | ❌ artefact | Runtime state (bridge PID/host/port). Gitignored. |
| `bridge.log` | ❌ artefact | Bridge subprocess stdout/stderr. Gitignored. |

The bridge **script** itself (`minecraft_bridge.js`) lives one level up
in the plugin folder and is executed with `NODE_PATH` pointing here, so
`require('mineflayer')` resolves against this folder's `node_modules`
regardless of the script's own location.

## Provisioning

`interface/minecraft_provisioner.py::BridgeProvisioner` manages the lifecycle:

- **install** — runs `npm install` into this folder (using the committed
  `package.json`) if any required dependency is missing.
- **start** — launches the bridge subprocess with `cwd` set here and
  `NODE_PATH` prepended with `node_modules`.

To pre-populate the runtime for a fully offline / self-contained zip, run once:

```bash
cd plugins/rift_vessel/minecraft/mineflayer
npm install --no-audit --no-fund
```

and include the resulting `node_modules/` in the packaged zip.
