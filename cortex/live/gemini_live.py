# cortex/live/gemini_live.py

"""Prototype Gemini Live engine for Cortex 'live' cortex.

This is an experimental, opt-in prototype. It provides a minimal
API surface so the rest of the system can interact with a live-capable
engine in a standardized way.
"""

from typing import Any, Optional, Dict
import asyncio
from core.logging_utils import log_debug, log_info, log_warning, log_error

CAPABILITIES = {
    "vision": True,
    "audio": True,
    "actions": True,
    "bidi": True,
    "low_latency": True,
}

class GeminiLivePlugin:
    display_name = "Gemini Live (Prototype)"

    def __init__(self, notify_fn=None):
        # Queues for outgoing sensory data
        self.vision_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.action_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self.running = False
        log_info("[gemini_live] Initialized Gemini Live prototype plugin")

    async def start_live_session(self, session_metadata: Optional[Dict[str, Any]] = None) -> None:
        """Start a live session (connect to Gemini Live bidi API in a real impl)."""
        if self.running:
            log_debug("[gemini_live] Live session already running")
            return
        self.running = True
        log_info("[gemini_live] Live session started (prototype)")

    async def stop_live_session(self) -> None:
        """Stop the live session and cleanup resources."""
        if not self.running:
            return
        self.running = False
        log_info("[gemini_live] Live session stopped (prototype)")

    async def bidi_stream_send(self, vision: Optional[bytes] = None, audio: Optional[bytes] = None) -> None:
        """Send vision/audio chunks to the engine (non-blocking)."""
        try:
            if vision:
                await self.vision_queue.put(vision)
            if audio:
                await self.audio_queue.put(audio)
        except Exception as e:
            log_warning(f"[gemini_live] Failed to enqueue sensory data: {e}")

    async def receive_actions(self, timeout: float = 0.0):
        """Receive an action suggested by the live model (non-blocking if timeout==0)."""
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(self.action_queue.get(), timeout=timeout)
            else:
                return self.action_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        except Exception as e:
            log_warning(f"[gemini_live] Error receiving action: {e}")
            return None

    # Minimal handler to show how actual engine would inject messages into message_chain
    async def handle_incoming_message(self, *args, **kwargs):
        """This will be called by plugin_instance same as other engines - return a normal response object/text."""
        # Prototype: echo a placeholder
        return {"text": "[gemini_live] prototype response"}


PLUGIN_CLASS = GeminiLivePlugin
