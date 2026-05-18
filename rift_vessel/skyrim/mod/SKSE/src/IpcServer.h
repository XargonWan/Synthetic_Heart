#pragma once

#include <string>
#include <thread>
#include <atomic>
#include <functional>
#include <mutex>

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

#include "Config.h"
#include "WorldState.h"

// ─── Callback when an action arrives from SyntH ─────────────────────────────
using ActionCallback = std::function<void(const std::string& action, const std::string& payload)>;

class IpcServer {
public:
    IpcServer(ActionCallback on_action)
        : m_on_action(std::move(on_action))
        , m_running(false)
    {}

    ~IpcServer() { stop(); }

    void start();
    void stop();

    // Called by the game thread to send the latest world state
    void publish_world_state(const WorldState& ws);

private:
    void server_loop();

#ifdef _WIN32
    void handle_windows_client(HANDLE pipe);
#else
    void handle_unix_client(int client_fd);
#endif

    ActionCallback m_on_action;
    std::thread m_thread;
    std::atomic<bool> m_running;

    std::mutex m_state_mutex;
    std::string m_pending_world_state;  // JSON
};
