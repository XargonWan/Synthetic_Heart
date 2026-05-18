// ─── Synthetic Heart — Skyrim Rift Vessel SKSE Plugin ───────────────────────
//
//  This SKSE plugin exposes an NPC that SyntH can control remotely via IPC.
//  It provides:
//    1. A background IPC server (named pipe / unix socket)
//    2. Papyrus functions for the NPC behaviour
//    3. World-state polling (location, health, nearby actors)
//
//  Build requirements:
//    - CommonLibSSE-NG
//    - spdlog
//    - nlohmann-json (included by CommonLibSSE)
//
// ═══════════════════════════════════════════════════════════════════════════════

#include "IpcServer.h"
#include "WorldState.h"

#include <chrono>
#include <thread>

// CommonLibSSE
#include <SKSE/SKSE.h>
#include <RE/Skyrim.h>
#include <RE/BSScript/IVirtualMachine.h>

using namespace SKSE;
using namespace RE;

// ─── Globals ─────────────────────────────────────────────────────────────────
static std::unique_ptr<IpcServer> g_ipc;
static WorldState                 g_world;

// ═══════════════════════════════════════════════════════════════════════════════
//  Papyrus bindings
// ═══════════════════════════════════════════════════════════════════════════════

namespace Papyrus {

    /// Called from the NPC's OnUpdate loop to push current game state.
    void UpdateWorldState(RE::StaticFunctionTag*) {
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return;

        // ── Location ──
        auto* loc = player->currentLocation;
        g_world.location = loc ? loc->GetFullName() : "";

        // ── Health / Magicka / Stamina ──
        auto* av = player->AsActorValueOwner();
        if (av) {
            g_world.health     = av->GetActorValue(ActorValue::kHealth);
            g_world.max_health = av->GetPermanentActorValue(ActorValue::kHealth);
            g_world.magicka    = av->GetActorValue(ActorValue::kMagicka);
            g_world.stamina    = av->GetActorValue(ActorValue::kStamina);
        }

        // ── Combat ──
        g_world.combat_state = player->IsInCombat();
        g_world.is_sneaking  = player->IsSneaking();
        g_world.is_mounted   = player->IsOnMount();

        // ── Equipment ──
        auto* rhs = player->GetEquippedObject(false);
        g_world.current_weapon = rhs ? rhs->GetName() : "";

        auto* lhs = player->GetEquippedObject(true);
        if (lhs && !rhs)
            g_world.current_spell = lhs->GetName();

        g_world.level = player->GetLevel();

        // ── Nearby actors ──
        g_world.visible_entities.clear();
        auto nearby = player->GetNearbyActors(4096.0f);
        for (auto* actor : nearby) {
            if (!actor || actor == player) continue;
            EntityRef ref;
            ref.id     = std::to_string(actor->GetFormID());
            ref.name   = actor->GetDisplayFullName();
            ref.distance = player->GetPosition().GetDistance(actor->GetPosition());
            ref.health_pct = actor->AsActorValueOwner()
                ? actor->AsActorValueOwner()->GetActorValue(ActorValue::kHealth) / 100.0f
                : -1.0f;
            g_world.visible_entities.push_back(ref);
        }

        // ── Push to IPC ──
        if (g_ipc)
            g_ipc->publish_world_state(g_world);
    }

    /// Execute an action received from SyntH (called from Papyrus polling).
    void ExecuteAction(RE::StaticFunctionTag*, std::string action, std::string payload) {
        // Delegate to the game thread via a task.
        SKSE::GetTaskInterface()->AddTask([action, payload]() {

            // ── Parse the action ──
            // This is where we map SyntH action names to Skyrim behaviour.

            if (action == "game_skyrim_attack") {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (player) {
                    // Find the target ActorRef from payload's "target" field
                    // and cast it into the Papyrus VM.
                    // (simplified — real implementation uses TESObjectREFR look-up)
                    player->NotifyAnimationGraph("attackStart");
                }
            }
            else if (action == "game_skyrim_cast_spell") {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (player) {
                    // Example: equip and cast the spell from payload["spell"]
                    // For now trigger a cast animation
                    player->NotifyAnimationGraph("magicCast");
                }
            }
            else if (action == "game_skyrim_shout") {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (player) {
                    player->NotifyAnimationGraph("shoutStart");
                }
            }
            else if (action == "game_skyrim_equip") {
                // Look up item name → TESForm → EquipItem
                // Stub for Phase 0
            }
            else if (action == "game_skyrim_use_item") {
                // Use a potion or scroll from inventory
            }
            else if (action == "game_skyrim_follow") {
                // Set the NPC's follow target to the player
                // (handled by Papyrus AI package)
            }
            else if (action == "game_skyrim_wait") {
                // Switch to sandbox / wait package
            }
            else if (action == "game_skyrim_move_to") {
                // Path to the given location
            }
        });
    }

}  // namespace Papyrus

// ═══════════════════════════════════════════════════════════════════════════════
//  SKSE Plugin entry
// ═══════════════════════════════════════════════════════════════════════════════

namespace {

    void RegisterPapyrusFunctions(RE::BSScript::IVirtualMachine* vm) {
        vm->RegisterFunction("UpdateWorldState", "SynthRiftVessel", Papyrus::UpdateWorldState);
        vm->RegisterFunction("ExecuteAction",    "SynthRiftVessel", Papyrus::ExecuteAction);
        log::info("[SynthRiftVessel] Papyrus functions registered.");
    }

    void OnDataLoaded() {
        log::info("[SynthRiftVessel] Data loaded — starting IPC server.");

        g_ipc = std::make_unique<IpcServer>([](const std::string& action,
                                                const std::string& payload) {
            // Called from IPC thread: enqueue for Papyrus callback.
            // In practice, we store the action and let the next OnUpdate
            // on the NPC's script pick it up via a Papyrus event.
            SKSE::GetTaskInterface()->AddTask([action, payload]() {
                // Dispatch to the NPC's script via a ModEvent or a simple
                // global variable that Papyrus polls.
            });
        });

        g_ipc->start();
        log::info("[SynthRiftVessel] IPC server started on {}", PIPE_NAME);
    }

}  // namespace

SKSEPluginLoad(const SKSE::LoadInterface* skse) {
    Init(skse);

    SKSE::GetMessagingInterface()->RegisterListener([](MessagingInterface::Message* msg) {
        switch (msg->type) {
        case MessagingInterface::kPostLoad:
            log::info("[SynthRiftVessel] SKSE plugin loaded.");
            break;
        case MessagingInterface::kDataLoaded:
            OnDataLoaded();
            break;
        }
    });

    SKSE::GetPapyrusInterface()->Register(RegisterPapyrusFunctions);

    return true;
}
