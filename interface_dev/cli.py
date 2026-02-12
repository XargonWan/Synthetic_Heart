import json
import threading
import queue
from core.logging_utils import log_debug, log_info
from core.plugin_base import PluginBase
from core.message_queue import MessageQueue
from core.core_initializer import register_interface, core_initializer

# Register event_type for CLI
EVENT_TYPE_CLI = "message_cli"
EVENT_TYPE_CLI_EXEC = "cli_exec"


class CLIInterface(PluginBase):
    def __init__(self):
        super().__init__()
        self.queue = MessageQueue()
        self.running = True
        self.thread = threading.Thread(target=self._listen_loop)
        self.thread.daemon = True
        self.thread.start()
        # Start the TCP server to receive messages from the CLI
        self.server_thread = threading.Thread(target=self._start_server)
        self.server_thread.daemon = True
        self.server_thread.start()
        log_info("[cli] CLI interface started")

    def _start_server(self, host="127.0.0.1", port=5555):
        import socket

        log_info(f"[cli] Starting CLI server on {host}:{port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(5)
            while self.running:
                try:
                    conn, addr = s.accept()
                    with conn:
                        data = b""
                        while True:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        if data:
                            try:
                                msg = json.loads(data.decode())
                                self.queue.put(msg)
                            except Exception as e:
                                log_debug(f"[cli] Error parsing CLI message: {e}")
                except Exception as e:
                    log_debug(f"[cli] CLI server error: {e}")

    def _listen_loop(self):
        while self.running:
            try:
                msg = self.queue.get(timeout=1)
                self.handle_message(msg)
            except queue.Empty:
                continue

    def handle_message(self, msg):
        log_debug(f"[cli] Received message: {msg}")
        if msg.get("type") == EVENT_TYPE_CLI:
            self.on_cli_message(msg)
        elif msg.get("type") == EVENT_TYPE_CLI_EXEC:
            self.on_cli_exec(msg)

    def on_cli_message(self, msg):
        # Here you can handle the response logic
        log_info(f"[cli] Message: {msg.get('text')}")
        # Simulate a response
        print(f"synth: {msg.get('text')}")

    def on_cli_exec(self, msg):
        command = msg.get("command")
        log_info(f"[cli] Exec: {command}")
        # Simulate command execution
        print(f"[EXEC] {command}")

    def send_message(self, text):
        msg = {"type": EVENT_TYPE_CLI, "text": text}
        self.queue.put(msg)

    def send_exec(self, command):
        msg = {"type": EVENT_TYPE_CLI_EXEC, "command": command}
        self.queue.put(msg)


INTERFACE_CLASS = CLIInterface


def start_cli_interface():
    """Instantiate and register the CLI interface with the core."""
    cli = CLIInterface()
    register_interface("cli", cli)
    core_initializer.register_interface("cli")
    return cli
