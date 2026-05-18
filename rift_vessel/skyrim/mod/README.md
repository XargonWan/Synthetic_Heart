# Synthetic Heart — Skyrim Rift Vessel

**Synth** — the digital person from the [Synthetic Heart](https://github.com/anomalyco/Synthetic_Heart) project — embodied inside Skyrim as a fully voiced, reactive NPC follower.

This mod provides the in-game side of the **Rift Vessel** bridge:
- An **SKSE plugin** (`synth_rift_vessel.dll`) that opens an IPC channel (named pipe / unix socket) between Skyrim and the SyntH backend.
- A **Papyrus quest** (`SynthRiftVessel.psc`) that drives the NPC's behaviour, polls world state, and dispatches actions from SyntH.
- An **ESL-flagged ESP** (or Creation Kit template) that places the NPC in the world.

---

## Architecture

```
┌─────────────────────────┐      IPC (pipe / socket)       ┌──────────────────────┐
│  Skyrim                 │ ◄────────────────────────────► │  SyntH Backend        │
│                         │                                 │                       │
│  SKSE Plugin            │  WorldState JSON ──────►       │  RiftVesselBridge     │
│  └ synth_rift_vessel.dll│  ◄────── Action JSON           │  └ SkyrimVessel       │
│                         │                                 │  └ prompt_engine      │
│  Papyrus Quest          │                                 │  └ LLM → JSON actions │
│  └ SynthRiftVessel      │                                 └──────────────────────┘
│  └ SynthNPC (Actor)     │
└─────────────────────────┘
```

World state flows: Skyrim → SKSE → pipe → SyntH → LLM context → JSON actions → pipe → SKSE → Papyrus → NPC.

---

## Files

```
rift_vessel/skyrim/mod/
├── SKSE/
│   ├── CMakeLists.txt              # CMake build for the SKSE plugin
│   └── src/
│       ├── Config.h                # IPC protocol constants
│       ├── IpcServer.h             # IPC server header
│       ├── IpcServer.cpp           # Named pipe / Unix socket server
│       ├── WorldState.h            # Skyrim world-state struct → JSON
│       └── SynthRiftVessel.cpp     # SKSE entry point + Papyrus bindings
├── Scripts/
│   └── Source/
│       └── SynthRiftVessel.psc     # Papyrus quest script
└── Docs/
    └── CREATE_NPC_GUIDE.md         # Creation Kit walk-through (TBD)
```

---

## Build Requirements

| Tool | Version | Notes |
|------|---------|-------|
| Visual Studio 2022 | ≥ 17.0 | Windows (native) |
| MinGW-w64 | ≥ 12.0 | Linux cross-compile: `x86_64-w64-mingw32-g++` |
| CMake | ≥ 3.20 | |
| vcpkg | latest | With `commonlibsse-ng` installed |
| Skyrim SE/AE | 1.6.x+ | Includes Anniversary Edition |
| SKSE | 2.2.x+ | |
| Address Library | ≥ 1.0 | SKSE plugin for version-independent offsets |
| Creation Kit | — | Only needed to place the NPC in the world |

### Installing vcpkg dependencies

```powershell
vcpkg install commonlibsse-ng spdlog fmt

# Or, if you use vcpkg manifest mode, add to vcpkg.json:
# {
#   "name": "synth-rift-vessel",
#   "version": "0.1.0",
#   "dependencies": [ "commonlibsse-ng", "spdlog", "fmt" ]
# }
```

---

## Building

```powershell
# 1. Set the Skyrim Data directory env var (adjust for your install path)
$env:SKYRIM_DATA_DIR = "C:/Program Files (x86)/Steam/steamapps/common/Skyrim Special Edition/Data"

# 2. Configure
cmake -B build `
    -DCMAKE_TOOLCHAIN_FILE="path/to/vcpkg/scripts/buildsystems/vcpkg.cmake" `
    -DCMAKE_BUILD_TYPE=Release

# 3. Build
cmake --build build --config Release

# 4. The DLL is automatically copied to:
#    %SKYRIM_DATA_DIR%/SKSE/Plugins/synth_rift_vessel.dll
```

---

## Linux Cross-Compilation (MinGW-w64)

Skyrim runs via Proton/Wine on Linux — the DLL is still a Windows PE, but you can build it entirely from Linux.

### Requirements

```bash
# Debian / Ubuntu
sudo apt install cmake g++-mingw-w64-x86-64 mingw-w64-tools ninja-build

# Arch Linux
sudo pacman -S mingw-w64-gcc cmake ninja

# Fedora
sudo dnf install mingw64-gcc-c++ cmake ninja-build
```

### Install vcpkg with MinGW triplet

```bash
# Clone vcpkg somewhere (one-time)
git clone https://github.com/Microsoft/vcpkg.git ~/vcpkg
~/vcpkg/bootstrap-vcpkg.sh

# Install commonlibsse-ng with the MinGW triplet
~/vcpkg/vcpkg install commonlibsse-ng spdlog fmt \
    --triplet x64-mingw-dynamic
```

> **Note**: CommonLibSSE-NG's MinGW support is community-maintained. If the triplet fails, try `x64-mingw-static` or see [github.com/alandtse/CommonLibSSE-NG/issues](https://github.com/alandtse/CommonLibSSE-NG/issues) for workarounds.

### Building

```bash
# 1. Configure with vcpkg's MinGW triplet
#    (vcpkg's toolchain auto-selects x86_64-w64-mingw32-g++ via triplet)
cmake -B build-mingw \
    -DCMAKE_BUILD_TYPE=Release \
    -DVCPKG_TARGET_TRIPLET=x64-mingw-dynamic \
    -DCMAKE_TOOLCHAIN_FILE="$HOME/vcpkg/scripts/buildsystems/vcpkg.cmake"

# 2. Build
cmake --build build-mingw -- -j$(nproc)

# 3. Output DLL
ls -la build-mingw/synth_rift_vessel.dll
```

### Deploy to Proton's Skyrim

```bash
# Find your Skyrim prefix
PROTON_PREFIX="$HOME/.steam/steam/steamapps/compatdata/489830"  # or adjust

# Copy the DLL
cp build-mingw/synth_rift_vessel.dll \
    "$PROTON_PREFIX/pfx/drive_c/users/steamuser/AppData/Local/Skyrim Special Edition/Data/SKSE/Plugins/"

# Compile Papyrus script (using the Creation Kit via Proton):
# Place SynthRiftVessel.psc in the Skyrim Data/Scripts/Source/ folder
# Then run via Proton:
#   WINEPREFIX=$PROTON_PREFIX wine "SkyrimSE.exe" -compilep "SynthRiftVessel"
```

> **Proton IPC note**: On Linux the SKSE plugin falls back to a Unix socket at `/tmp/synth-rift-vessel.sock` instead of a Windows named pipe. The SyntH `SkyrimVessel` auto-detects the platform — no config change needed.

### Docker-based build (alternative)

```bash
# Use a pre-configured MinGW Docker image for reproducibility
docker run --rm -v "$(pwd):/src" -w /src \
    ghcr.io/steam-test1/mingw-commonlibsse:latest \
    bash -c "cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build"
```

---

## Post-build check

Verify the plugin loads:

```powershell
# Check the SKSE log (My Games/Skyrim Special Edition/SKSE/synth_rift_vessel.log):
# > [SynthRiftVessel] SKSE plugin loaded.
# > [SynthRiftVessel] Data loaded — starting IPC server.
# > [SynthRiftVessel] IPC server started on \\.\pipe\SynthRiftVessel
```

---

## Creating the NPC in the Creation Kit

### Quick start:

1. **Open Creation Kit** → load `Skyrim.esm` + `Update.esm` + `Dawnguard.esm` + `HearthFires.esm` + `Dragonborn.esm`
2. **New Quest**: `SynthRiftVesselQuest` — tick "Start Game Enabled"
3. **Add script**: `SynthRiftVessel` (the `.pex` compiled from `Scripts/Source/`)
4. **Set property**: `SynthNPC` → click "Edit Value" → select an Actor
5. **Create Actor**: duplicate any NPC (e.g. `Lydia`), give them a unique Editor ID (e.g. `SynthFollower`)
6. **Add AI Package**: `Follow` → set subject to the player
7. **Place the NPC** in the world (e.g. Whiterun's Bannered Mare)
8. **Save as ESP** (or ESL-flag it)

> A step-by-step guide with screenshots is planned (`Docs/CREATE_NPC_GUIDE.md` — TBD).

---

## Testing

### 1. Start SyntH with the Rift Vessel bridge active

```bash
# The bridge auto-registers when rift_vessel is importable.
# Verify:
docker exec synth-dev tail -f /app/logs/synth.log | grep "rift"
# Expected: "RiftVesselBridge registered in INTERFACE_REGISTRY"
```

### 2. Launch Skyrim with the mod enabled

- Ensure `synth_rift_vessel.dll` is in `Data/SKSE/Plugins/`
- Ensure `SynthRiftVessel.pex` is in `Data/Scripts/`
- Ensure the ESP is enabled in your mod manager
- Load a save near the NPC

### 3. Verify the IPC connection

Check the SKSE log:
```
[SynthRiftVessel] IPC server started on \.\pipe\SynthRiftVessel
```

SyntH will now see the NPC's world state in its prompt context:
```
[Embodied world state]
┌─ skyrim ────────────────────────────
│ SynthFollower — Whiterun
│ HP: 100/100  Magicka: 100  Stamina: 100
│ Nearby: Lydia (friendly, 2.5m), Farengar (neutral, 8.1m)
└────────────────────────────────────
```

### 4. Send a command

Any `game_skyrim_*` action from the LLM will route through the bridge → IPC → SKSE → Papyrus → NPC.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `IPC server started` not in SKSE log | DLL not deployed or missing dependencies |
| NPC stands still / does not react | Papyrus quest not started (`StartQuest SynthRiftVesselQuest` in console) |
| `World state: null` in SyntH logs | IPC pipe connected but `UpdateWorldState()` not called (check `PollInterval` property) |
| `Named pipe not found` in SyntH | Skyrim not running with the plugin, or wrong pipe name |
| Actions never reach NPC | Check `SynthRiftVessel.pending_action` clearance in `OnUpdate` |

---

## Future Development

- **Phase 1**: Basic NPC with full action set (attack / follow / wait / equip / use items)
- **Phase 2**: World events → SyntH memory; quest state awareness
- **Phase 3**: Lip-synced dialogue; contextual Shouts / spells; dynamic AI packages
- **Phase 4**: Full custom-voiced follower with Rift Vessel dialogue engine

---

## License

This mod is part of Synthetic Heart. When published to Nexus Mods, it will be under **MIT License**.
