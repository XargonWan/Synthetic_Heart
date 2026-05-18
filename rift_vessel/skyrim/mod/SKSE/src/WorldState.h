#pragma once

#include <string>
#include <vector>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

struct EntityRef {
    std::string id;
    std::string name;
    std::string relationship = "neutral";
    float health_pct = -1.0f;
    float distance = -1.0f;

    json to_json() const {
        json j;
        j["id"] = id;
        j["name"] = name;
        j["relationship"] = relationship;
        if (health_pct >= 0) j["health_pct"] = health_pct;
        if (distance >= 0)   j["distance"] = distance;
        return j;
    }
};

struct WorldState {
    std::string entity_id  = "synth_npc";
    std::string location;

    float health     = 100.0f;
    float max_health = 100.0f;
    float magicka    = 100.0f;
    float stamina    = 100.0f;

    bool combat_state = false;
    bool is_sneaking  = false;
    bool is_mounted   = false;

    std::string current_weapon;
    std::string current_spell;
    std::string current_shout;
    int level = 1;
    float carry_weight_pct = 0.0f;
    int gold = 0;

    std::vector<EntityRef> visible_entities;
    std::vector<std::string> recent_dialogue;

    // ═══ Serialise to JSON used by the IPC protocol ═══════════════════════════
    json to_json() const {
        json j;
        j["environment"]   = "skyrim";
        j["entity_id"]     = entity_id;
        j["location"]      = location;
        j["health"]        = health;
        j["max_health"]    = max_health;
        j["magicka"]       = magicka;
        j["stamina"]       = stamina;
        j["combat_state"]  = combat_state;
        j["is_sneaking"]   = is_sneaking;
        j["is_mounted"]    = is_mounted;
        j["current_weapon"] = current_weapon;
        j["current_spell"]  = current_spell;
        j["current_shout"]  = current_shout;
        j["level"]          = level;
        j["carry_weight_pct"] = carry_weight_pct;
        j["gold"]           = gold;

        json arr = json::array();
        for (const auto& e : visible_entities)
            arr.push_back(e.to_json());
        j["visible_entities"] = arr;

        j["recent_dialogue"] = recent_dialogue;
        return j;
    }
};
