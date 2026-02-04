"""
Example Development Interface
==============================

This is an example interface in the interface_dev/ directory.
It will only be loaded when dev components are enabled in the WebUI.

This file demonstrates:
1. How to create a minimal dev interface
2. How to use the standard interface registration pattern
3. How to handle startup/shutdown gracefully
"""

from core.interface_adapters import BaseInterface, register_interface
from core.logging_utils import log_info, log_warning, log_error

INTERFACE_NAME = "example_dev_interface"


class ExampleDevInterface(BaseInterface):
    """
    Example development interface for testing.

    This interface doesn't do anything useful - it just logs messages
    to demonstrate that the dev components system is working.
    """

    def __init__(self):
        super().__init__(INTERFACE_NAME)
        self.is_running = False
        log_info(f"[{INTERFACE_NAME}] 🔧 Dev interface initialized")

    async def start(self):
        """Start the dev interface (just logs a message)."""
        if self.is_running:
            log_warning(f"[{INTERFACE_NAME}] Already running")
            return

        log_info(f"[{INTERFACE_NAME}] ⚠️ Starting DEVELOPMENT interface")
        log_info(f"[{INTERFACE_NAME}] This is an example dev component")
        log_info(f"[{INTERFACE_NAME}] It only loads when dev_components_enabled=True")
        self.is_running = True

    async def stop(self):
        """Stop the dev interface."""
        if not self.is_running:
            return

        log_info(f"[{INTERFACE_NAME}] Stopping dev interface")
        self.is_running = False

    def get_interface_instructions(self) -> str:
        """Return description for WebUI."""
        return (
            "Example development interface. This appears only when "
            "dev components are enabled. Used for testing the dev "
            "components system."
        )


# Auto-register when module is imported
try:
    register_interface(INTERFACE_NAME, ExampleDevInterface())
    log_info(f"[{INTERFACE_NAME}] ✅ Dev interface registered")
except Exception as e:
    log_error(f"[{INTERFACE_NAME}] ❌ Failed to register: {e}")
