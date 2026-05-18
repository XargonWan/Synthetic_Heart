#include "IpcServer.h"

#include <iostream>
#include <sstream>
#include <cstring>
#include <nlohmann/json.hpp>   // included by CommonLibSSE or vendored
using json = nlohmann::json;

// ═══════════════════════════════════════════════════════════════════════════════
//  IPC protocol (line-based JSON over a stream)
//
//  SyntH → Skyrim (commands):
//    {"cmd":"execute_action","action":"game_skyrim_attack","payload":{...}}\n
//    {"cmd":"get_state"}\n
//
//  Skyrim → SyntH (responses):
//    {"status":"ok","action":"game_skyrim_attack",...}\n
//    {"status":"state","world_state":{...}}\n
// ═══════════════════════════════════════════════════════════════════════════════

void IpcServer::start() {
    m_running = true;
    m_thread = std::thread(&IpcServer::server_loop, this);
}

void IpcServer::stop() {
    m_running = false;
    if (m_thread.joinable())
        m_thread.join();
}

void IpcServer::publish_world_state(const WorldState& ws) {
    json j = ws.to_json();
    std::lock_guard<std::mutex> lock(m_state_mutex);
    m_pending_world_state = j.dump();
}

// ─── Server loop ─────────────────────────────────────────────────────────────

void IpcServer::server_loop() {
#ifdef _WIN32
    // Windows: named pipe
    HANDLE pipe = CreateNamedPipeA(
        PIPE_NAME,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES,
        4096, 4096, 0, nullptr
    );
    if (pipe == INVALID_HANDLE_VALUE) {
        // Fall back to TCP on failure
        return;
    }

    while (m_running) {
        BOOL connected = ConnectNamedPipe(pipe, nullptr)
                         ? TRUE
                         : (GetLastError() == ERROR_PIPE_CONNECTED);
        if (!connected) break;

        handle_windows_client(pipe);

        DisconnectNamedPipe(pipe);
    }
    CloseHandle(pipe);

#else
    // Linux / Proton: Unix domain socket
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return;

    struct sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, PIPE_NAME, sizeof(addr.sun_path) - 1);
    unlink(PIPE_NAME);

    if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        close(fd);
        return;
    }
    listen(fd, 5);

    while (m_running) {
        int client = accept(fd, nullptr, nullptr);
        if (client < 0) continue;
        handle_unix_client(client);
        close(client);
    }
    close(fd);
    unlink(PIPE_NAME);
#endif
}

// ─── Windows named-pipe client handler ───────────────────────────────────────
#ifdef _WIN32
void IpcServer::handle_windows_client(HANDLE pipe) {
    char buf[4096];
    DWORD read{};

    while (m_running && ReadFile(pipe, buf, sizeof(buf) - 1, &read, nullptr)) {
        buf[read] = '\0';
        std::string line(buf);

        try {
            auto msg = json::parse(line);

            if (msg["cmd"] == "get_state") {
                std::lock_guard<std::mutex> lock(m_state_mutex);
                std::string reply = m_pending_world_state.empty()
                    ? R"({"status":"state","world_state":null})"
                    : R"({"status":"state","world_state":)" + m_pending_world_state + "}";
                DWORD written{};
                WriteFile(pipe, reply.data(), (DWORD)reply.size(), &written, nullptr);
                continue;
            }

            if (msg["cmd"] == "execute_action") {
                std::string action = msg.value("action", "");
                std::string payload = msg.value("payload", json::object()).dump();
                if (m_on_action)
                    m_on_action(action, payload);
                std::string ok = R"({"status":"ok"})";
                DWORD written{};
                WriteFile(pipe, ok.data(), (DWORD)ok.size(), &written, nullptr);
                continue;
            }

        } catch (...) {}
    }
}
#endif

// ─── Unix client handler ─────────────────────────────────────────────────────
#ifndef _WIN32
void IpcServer::handle_unix_client(int client_fd) {
    char buf[4096];
    while (m_running) {
        ssize_t n = read(client_fd, buf, sizeof(buf) - 1);
        if (n <= 0) break;
        buf[n] = '\0';
        std::string line(buf);

        try {
            auto msg = json::parse(line);

            if (msg["cmd"] == "get_state") {
                std::lock_guard<std::mutex> lock(m_state_mutex);
                std::string reply = m_pending_world_state.empty()
                    ? R"({"status":"state","world_state":null})"
                    : R"({"status":"state","world_state":)" + m_pending_world_state + "}";
                write(client_fd, reply.data(), reply.size());
                continue;
            }

            if (msg["cmd"] == "execute_action") {
                std::string action = msg.value("action", "");
                std::string payload = msg.value("payload", json::object()).dump();
                if (m_on_action)
                    m_on_action(action, payload);
                std::string ok = R"({"status":"ok"})";
                write(client_fd, ok.data(), ok.size());
                continue;
            }

        } catch (...) {}
    }
}
#endif
