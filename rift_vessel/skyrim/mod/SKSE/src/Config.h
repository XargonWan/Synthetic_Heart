#pragma once

// ── IPC protocol version ─────────────────────────────────────────────────────
constexpr int SYNTH_RIFT_PROTOCOL_VERSION = 1;

// ── Named pipe name (Windows) / unix socket path (Proton) ────────────────────
#ifdef _WIN32
constexpr const char* PIPE_NAME = R"(\\.\pipe\SynthRiftVessel)";
#else
constexpr const char* PIPE_NAME = "/tmp/synth-rift-vessel.sock";
#endif

// ── World state JSON keys ────────────────────────────────────────────────────
constexpr const char* WS_ENVIRONMENT  = "skyrim";
constexpr const char* WS_ENTITY_ID    = "synth_npc";
