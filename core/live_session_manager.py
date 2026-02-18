# core/live_session_manager.py

"""Gemini Live API session manager for real-time audio interactions.

Manages WebSocket sessions to the Gemini Live API, handling:
- Session lifecycle (connect, reconnect, disconnect)
- Persona/system instruction injection at session start
- Bidirectional audio streaming (PCM 16kHz mono)
- Function call routing to the SyntH action pipeline
- Session duration tracking and automatic reconnection

Audio format: PCM 16-bit signed little-endian, 16000 Hz, mono.
This matches both Discord.py's voice receive format and the Gemini Live API's
expected input format.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from core.logging_utils import log_debug, log_error, log_info, log_warning

# PCM audio constants for Gemini Live API
LIVE_AUDIO_SAMPLE_RATE = 16000
LIVE_AUDIO_MIME = f"audio/pcm;rate={LIVE_AUDIO_SAMPLE_RATE}"
# Gemini outputs 24kHz PCM by default
LIVE_OUTPUT_SAMPLE_RATE = 24000
LIVE_OUTPUT_MIME = f"audio/pcm;rate={LIVE_OUTPUT_SAMPLE_RATE}"

# Session limits (from Google docs)
MAX_AUDIO_SESSION_SECONDS = 15 * 60  # 15 minutes audio-only
RECONNECT_BUFFER_SECONDS = 30  # reconnect 30s before limit

# Live API model
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

try:
    from google import genai
    from google.genai import types

    _HAS_GENAI_SDK = True
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _HAS_GENAI_SDK = False


class LiveSessionState:
    """Tracks the state of a single Live API session."""

    __slots__ = (
        "session_id",
        "guild_id",
        "channel_id",
        "started_at",
        "is_active",
        "_session",
        "_receive_task",
    )

    def __init__(
        self,
        session_id: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        self.session_id = session_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.started_at: float = time.monotonic()
        self.is_active: bool = False
        self._session: Any = None  # genai AsyncSession
        self._receive_task: asyncio.Task[None] | None = None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def should_reconnect(self) -> bool:
        return self.elapsed_seconds >= (
            MAX_AUDIO_SESSION_SECONDS - RECONNECT_BUFFER_SECONDS
        )


class LiveSessionManager:
    """Manages Gemini Live API sessions for Discord voice channels.

    One session per guild — when the bot joins a voice channel in a guild,
    a Live API WebSocket session is opened. When it leaves, the session closes.
    """

    def __init__(self, api_key: str) -> None:
        if not _HAS_GENAI_SDK:
            raise RuntimeError(
                "google-genai SDK required for Live API. Install with: uv add google-genai"
            )
        self._client = genai.Client(
            api_key=api_key.strip(),
            http_options={"api_version": "v1alpha"},
        )
        # guild_id -> LiveSessionState
        self._sessions: dict[int, LiveSessionState] = {}
        # Callbacks
        self._on_audio: Callable[[int, bytes], Awaitable[None]] | None = None
        self._on_text: Callable[[int, str], Awaitable[None]] | None = None
        self._on_tool_call: Callable[[int, dict], Awaitable[dict]] | None = None
        self._reconnect_locks: dict[int, asyncio.Lock] = {}

    def set_audio_callback(
        self, callback: Callable[[int, bytes], Awaitable[None]]
    ) -> None:
        """Set callback for audio received from the model. Args: (guild_id, pcm_bytes)."""
        self._on_audio = callback

    def set_text_callback(
        self, callback: Callable[[int, str], Awaitable[None]]
    ) -> None:
        """Set callback for text received from the model. Args: (guild_id, text)."""
        self._on_text = callback

    def set_tool_call_callback(
        self, callback: Callable[[int, dict], Awaitable[dict]]
    ) -> None:
        """Set callback for tool/function calls. Args: (guild_id, call_dict) -> response_dict."""
        self._on_tool_call = callback

    async def start_session(
        self,
        guild_id: int,
        channel_id: int,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Start a Live API session for a guild's voice channel.

        Args:
            guild_id: Discord guild ID (one session per guild).
            channel_id: Discord voice channel ID.
            system_instruction: Full persona/system prompt text.
            tools: Optional function declarations for the Live API.

        Returns:
            True if session started successfully.
        """
        if guild_id in self._sessions and self._sessions[guild_id].is_active:
            log_warning(
                f"[live_session] Session already active for guild {guild_id}, closing first"
            )
            await self.stop_session(guild_id)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],  # type: ignore[arg-type]
            system_instruction=system_instruction,
        )
        if tools:
            live_config.tools = tools  # type: ignore[assignment]

        state = LiveSessionState(
            session_id=f"live_{guild_id}_{int(time.time())}",
            guild_id=guild_id,
            channel_id=channel_id,
        )

        try:
            session_ctx = self._client.aio.live.connect(
                model=LIVE_MODEL,
                config=live_config,
            )
            session = await session_ctx.__aenter__()
            state._session = session
            state.is_active = True
            # Store the context manager so we can __aexit__ later
            state._ctx = session_ctx  # type: ignore[attr-defined]

            self._sessions[guild_id] = state
            self._reconnect_locks.setdefault(guild_id, asyncio.Lock())

            # Start receive loop
            state._receive_task = asyncio.create_task(
                self._receive_loop(guild_id),
                name=f"live_receive_{guild_id}",
            )

            log_info(
                f"[live_session] Session started for guild {guild_id} "
                f"channel {channel_id} (model={LIVE_MODEL})"
            )
            return True

        except Exception as e:
            log_error(
                f"[live_session] Failed to start session for guild {guild_id}: {e}"
            )
            state.is_active = False
            return False

    async def stop_session(self, guild_id: int) -> None:
        """Stop the Live API session for a guild."""
        state = self._sessions.pop(guild_id, None)
        if not state:
            return

        state.is_active = False

        if state._receive_task and not state._receive_task.done():
            state._receive_task.cancel()
            try:
                await state._receive_task
            except asyncio.CancelledError:
                pass

        if state._session:
            try:
                ctx = getattr(state, "_ctx", None)
                if ctx:
                    await ctx.__aexit__(None, None, None)
                else:
                    await state._session.close()
            except Exception as e:
                log_warning(
                    f"[live_session] Error closing session for guild {guild_id}: {e}"
                )

        log_info(f"[live_session] Session stopped for guild {guild_id}")

    async def send_audio(self, guild_id: int, pcm_data: bytes) -> None:
        """Send PCM audio data to the Live API session.

        Args:
            guild_id: Target guild's session.
            pcm_data: Raw PCM 16-bit LE, 16kHz, mono audio bytes.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active or not state._session:
            return

        # Check if session needs reconnection
        if state.should_reconnect:
            asyncio.create_task(self._reconnect(guild_id))
            return

        try:
            await state._session.send_realtime_input(
                audio=types.Blob(data=pcm_data, mime_type=LIVE_AUDIO_MIME)
            )
        except Exception as e:
            log_warning(
                f"[live_session] Failed to send audio for guild {guild_id}: {e}"
            )

    async def send_text(self, guild_id: int, text: str) -> None:
        """Send a text message into the Live API session context.

        Useful for injecting context updates (e.g., emotion changes, diary entries)
        mid-session without using audio.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active or not state._session:
            return

        try:
            await state._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )
        except Exception as e:
            log_warning(f"[live_session] Failed to send text for guild {guild_id}: {e}")

    def is_session_active(self, guild_id: int) -> bool:
        """Check if a Live API session is active for a guild."""
        state = self._sessions.get(guild_id)
        return bool(state and state.is_active)

    def get_active_sessions(self) -> list[int]:
        """Return list of guild IDs with active sessions."""
        return [gid for gid, s in self._sessions.items() if s.is_active]

    async def _receive_loop(self, guild_id: int) -> None:
        """Background task: receive messages from the Live API and dispatch callbacks."""
        state = self._sessions.get(guild_id)
        if not state or not state._session:
            return

        try:
            async for message in state._session.receive():
                if not state.is_active:
                    break

                server_content = getattr(message, "server_content", None)
                tool_call = getattr(message, "tool_call", None)

                # Handle audio/text responses
                if server_content:
                    model_turn = getattr(server_content, "model_turn", None)
                    if model_turn and model_turn.parts:
                        for part in model_turn.parts:
                            # Audio output
                            inline_data = getattr(part, "inline_data", None)
                            if inline_data and inline_data.data and self._on_audio:
                                try:
                                    await self._on_audio(guild_id, inline_data.data)
                                except Exception as e:
                                    log_warning(
                                        f"[live_session] Audio callback error: {e}"
                                    )

                            # Text output
                            text = getattr(part, "text", None)
                            if text and self._on_text:
                                try:
                                    await self._on_text(guild_id, text)
                                except Exception as e:
                                    log_warning(
                                        f"[live_session] Text callback error: {e}"
                                    )

                # Handle function/tool calls
                if tool_call and self._on_tool_call:
                    function_calls = getattr(tool_call, "function_calls", []) or []
                    for fc in function_calls:
                        fc_name: str = str(getattr(fc, "name", ""))
                        fc_id: str = str(getattr(fc, "id", ""))
                        call_dict = {
                            "name": fc_name,
                            "id": fc_id,
                            "args": getattr(fc, "args", {}),
                        }
                        try:
                            result = await self._on_tool_call(guild_id, call_dict)
                            # Send tool response back
                            await state._session.send_tool_response(
                                function_responses=types.FunctionResponse(
                                    name=fc_name,
                                    id=fc_id,
                                    response=result or {"status": "ok"},
                                )
                            )
                        except Exception as e:
                            log_warning(f"[live_session] Tool call handling error: {e}")

        except asyncio.CancelledError:
            log_debug(f"[live_session] Receive loop cancelled for guild {guild_id}")
        except Exception as e:
            log_error(f"[live_session] Receive loop error for guild {guild_id}: {e}")
            state.is_active = False

    async def _reconnect(self, guild_id: int) -> None:
        """Reconnect a session approaching the time limit.

        Rebuilds the system instruction from the current persona and
        re-discovers tool declarations so function calling is preserved
        across session boundaries.
        """
        lock = self._reconnect_locks.get(guild_id)
        if not lock:
            return
        if lock.locked():
            return  # Already reconnecting

        async with lock:
            state = self._sessions.get(guild_id)
            if not state or not state.is_active:
                return

            log_info(
                f"[live_session] Reconnecting session for guild {guild_id} "
                f"(elapsed {state.elapsed_seconds:.0f}s)"
            )

            channel_id = state.channel_id
            await self.stop_session(guild_id)

            # Rebuild system instruction from current persona for the new session
            from core.prompt_engine import build_live_system_instruction

            instruction = await build_live_system_instruction()

            # Re-discover tool declarations so function calling persists
            tools: list[dict[str, Any]] | None = None
            try:
                from interface.discord_interface import _build_gemini_tool_declarations

                tools = _build_gemini_tool_declarations()
            except Exception as e:
                log_warning(
                    f"[live_session] Could not rebuild tool declarations on reconnect: {e}"
                )

            await self.start_session(
                guild_id=guild_id,
                channel_id=channel_id,
                system_instruction=instruction,
                tools=tools,
            )

    async def reconnect_with_instruction(
        self,
        guild_id: int,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Explicitly reconnect a session with a fresh system instruction.

        Called by the Discord interface when it detects a session nearing timeout
        or when persona/context has changed significantly.
        """
        state = self._sessions.get(guild_id)
        if not state:
            return False

        channel_id = state.channel_id
        await self.stop_session(guild_id)
        return await self.start_session(
            guild_id=guild_id,
            channel_id=channel_id,
            system_instruction=system_instruction,
            tools=tools,
        )

    async def close_all(self) -> None:
        """Close all active sessions. Call on shutdown."""
        guild_ids = list(self._sessions.keys())
        for guild_id in guild_ids:
            await self.stop_session(guild_id)
        log_info("[live_session] All sessions closed")
