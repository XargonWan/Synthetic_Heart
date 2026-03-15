# core/live_session_manager.py

"""Gemini Live API session manager for real-time audio interactions.

Manages WebSocket sessions to the Gemini Live API, handling:
- Session lifecycle (connect, reconnect, disconnect)
- Persona/system instruction injection at session start
- Bidirectional audio streaming (PCM 16kHz mono)
- Function call routing to the SyntH action pipeline
- Session duration tracking and automatic reconnection

Audio format: PCM 16-bit signed little-endian, 16000 Hz, mono.
Discord provides 48kHz stereo; the sink downsamples to 16kHz mono before
sending to the Gemini Live API.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Awaitable, ClassVar

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.live_api_logger import log_live_send, log_live_recv, log_live_session_event

# PCM audio constants for Gemini Live API
# Discord provides 48kHz PCM; we downsample to 16kHz before sending.
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

# Default voice — configurable via LIVE_VOICE_NAME in persona.json or .env.
# Available prebuilt voices: Puck, Charon, Kore, Fenrir, Aoede, Orbit,
# Zephyr, Leda, Orus, Autonoe.  Check Google's docs for the full current list.
_DEFAULT_VOICE = "Aoede"

try:
    from google import genai
    from google.genai import types

    _HAS_GENAI_SDK = True
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _HAS_GENAI_SDK = False


def _clean_transcript(parts: list[str]) -> str:
    """Join transcript fragments and normalise the result.

    Fragments are concatenated without an extra separator because Gemini
    streams them with their own surrounding whitespace.  We then strip
    emotion-state tags injected by the persona engine and collapse any
    runs of multiple spaces left behind.
    """
    text = "".join(parts).strip()
    # Remove emotion/state tags: {emotion neutral  intensity  3.0}
    text = re.sub(r"\{[^}]*\}", "", text)
    # Collapse runs of whitespace to a single space
    text = re.sub(r" {2,}", " ", text).strip()
    return text


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
        "_ctx",
        # History/caching tracking
        "last_injected_ts",
        "generating",  # whether a model turn is currently in progress
        "pending_context_updates",  # queued texts awaiting flush
        "_user_speaking",  # whether we have sent activity_start (manual VAD)
        "attachment_context",  # document text for system instruction (survives reconnects)
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
        # Cursor for incremental guild-history sync loop
        # (not used by context-update hook)
        self.last_injected_ts: str | None = None
        # live-turn state
        self.generating: bool = False
        self.pending_context_updates: list[str] = []
        self._user_speaking: bool = (
            False  # manual VAD: True between activity_start/activity_end
        )
        # Document context embedded in system instruction (persisted for reconnects)
        self.attachment_context: str | None = None

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

    This class is instantiated by the Cortex live engines but is also used
    directly by tests and by memory_search/discord_interface.  For convenience
    we provide a simple singleton accessor that lazily creates the manager.
    """

    # singleton instance used by helpers and tests
    _instance: ClassVar["LiveSessionManager | None"] = None

    @classmethod
    def get_instance(cls, api_key: str | None = None) -> "LiveSessionManager":
        """Return (and lazily create) a global LiveSessionManager.

        The manager requires an ``api_key`` argument for its constructor.  When
        invoked without one we attempt to read ``LIVE_API_KEY`` from the
        configuration registry.  This mirrors the behaviour in
        ``cortex.live.live_base.LiveSessionManager`` and keeps tests simple
        (they can monkeypatch this method directly).
        """
        if cls._instance is None:
            if api_key is None:
                try:
                    from core.config_manager import config_registry

                    api_key = str(config_registry.get_value("LIVE_API_KEY", "") or "")
                except Exception:
                    api_key = ""
            cls._instance = cls(api_key=api_key or "")
        return cls._instance

    # How often (seconds) the flush task drains accumulated audio to Gemini.
    _FLUSH_INTERVAL_S = 0.2  # 200ms

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
        self._on_turn_complete: Callable[[int, str, str], Awaitable[None]] | None = None
        self._reconnect_locks: dict[int, asyncio.Lock] = {}
        # Audio send buffers: guild_id -> bytearray
        self._send_buffers: dict[int, bytearray] = {}
        # Periodic flush tasks: guild_id -> Task
        self._flush_tasks: dict[int, asyncio.Task[None]] = {}
        # Periodic history sync tasks: guild_id -> Task
        self._sync_tasks: dict[int, asyncio.Task[None]] = {}

        # Live chat history sync configuration (updated by config listeners)
        from core.config_manager import config_registry

        # initial values
        self.sync_history: bool = bool(
            config_registry.get_value("LIVE_SYNC_CHAT_HISTORY", True, value_type=bool)
        )
        self.history_sync_interval: int = int(
            config_registry.get_value("LIVE_HISTORY_SYNC_INTERVAL", 30, value_type=int)
        )

        # keep attributes up-to-date if config changes
        def _on_sync_enabled(v: Any) -> None:
            self.sync_history = bool(v)
            if self.sync_history:
                # start loops for any active sessions that lack one
                for gid, state in list(self._sessions.items()):
                    if gid not in self._sync_tasks and getattr(
                        state, "is_active", False
                    ):
                        self._sync_tasks[gid] = asyncio.create_task(
                            self._history_sync_loop(gid),
                            name=f"live_history_sync_{gid}",
                        )
            else:
                # cancel all existing sync tasks
                for t in list(self._sync_tasks.values()):
                    t.cancel()
                self._sync_tasks.clear()

        config_registry.add_listener("LIVE_SYNC_CHAT_HISTORY", _on_sync_enabled)
        config_registry.add_listener(
            "LIVE_HISTORY_SYNC_INTERVAL",
            lambda v: setattr(self, "history_sync_interval", int(v)),
        )

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

    def set_turn_complete_callback(
        self, callback: Callable[[int, str, str], Awaitable[None]]
    ) -> None:
        """Set callback fired after each model turn completes.

        Args: (guild_id, user_transcript, model_transcript) — both sides of the turn.
        user_transcript comes from input_audio_transcription; model_transcript from
        output_audio_transcription. Either may be empty if transcription is unavailable.
        """
        self._on_turn_complete = callback

    async def start_session(
        self,
        guild_id: int,
        channel_id: int,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        attachment_context: str | None = None,
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

        # Resolve the voice name: persona config → .env → hardcoded default.
        _voice_name = _DEFAULT_VOICE
        try:
            from core.config_manager import config_registry

            _v = config_registry.get_value("LIVE_VOICE_NAME", None)
            if _v and isinstance(_v, str) and _v.strip():
                _voice_name = _v.strip()
        except Exception:
            pass

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],  # type: ignore[arg-type]
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_voice_name
                    )
                )
            ),
            # Disable server-side VAD and use manual activity_start/activity_end
            # signals instead.  Discord stops sending RTP packets when users are
            # silent, which freezes the server-side VAD clock and prevents it from
            # ever detecting the end of a turn.  Manual VAD gives us full control:
            # we send activity_start when we first receive Discord audio and
            # activity_end after a configurable silence timeout.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,
                ),
            ),
            system_instruction=system_instruction,
        )
        if tools:
            live_config.tools = tools  # type: ignore[assignment]

        state = LiveSessionState(
            session_id=f"live_{guild_id}_{int(time.time())}",
            guild_id=guild_id,
            channel_id=channel_id,
        )
        state.attachment_context = attachment_context

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

            log_live_session_event(
                guild_id,
                "start",
                f"model={LIVE_MODEL} voice={_voice_name} "
                f"system_instruction={len(system_instruction)} chars",
            )
            log_live_send(
                guild_id,
                msg_type="system_instruction",
                content=system_instruction,
            )

            # Start receive loop
            state._receive_task = asyncio.create_task(
                self._receive_loop(guild_id),
                name=f"live_receive_{guild_id}",
            )

            # Start periodic audio flush task
            self._flush_tasks[guild_id] = asyncio.create_task(
                self._audio_flush_loop(guild_id),
                name=f"live_flush_{guild_id}",
            )
            # Optionally start history sync loop
            if self.sync_history:
                self._sync_tasks[guild_id] = asyncio.create_task(
                    self._history_sync_loop(guild_id),
                    name=f"live_history_sync_{guild_id}",
                )

            log_info(
                f"[live_session] Session started for guild {guild_id} "
                f"channel {channel_id} (model={LIVE_MODEL})"
            )

            # Kick off the model with an initial text turn so it sends a greeting
            # without waiting for user audio. The system instruction handles the persona.
            try:
                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text="[Session started. Greet naturally.]")],
                    ),
                    turn_complete=True,
                )
                log_info(
                    f"[live_session] Sent initial kick to model for guild {guild_id}"
                )
            except Exception as e:
                log_warning(f"[live_session] Initial kick failed (non-fatal): {e}")

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
        self._send_buffers.pop(guild_id, None)

        # Cancel the flush task
        flush_task = self._flush_tasks.pop(guild_id, None)
        if flush_task and not flush_task.done():
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
        # Cancel the history sync task if present
        sync_task = self._sync_tasks.pop(guild_id, None)
        if sync_task and not sync_task.done():
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass

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
        """Buffer PCM audio data for the Live API session.

        Audio is accumulated here and flushed by ``_audio_flush_loop``
        every ~200ms.  This avoids flooding the WebSocket with many tiny
        messages (which can cause keepalive ping timeouts) while still
        ensuring speech tails are sent promptly.

        Args:
            guild_id: Target guild's session.
            pcm_data: Raw PCM 16-bit LE, 16kHz, mono audio bytes.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active:
            return

        buf = self._send_buffers.get(guild_id)
        if buf is None:
            buf = bytearray()
            self._send_buffers[guild_id] = buf
            log_debug(
                f"[live_session] First audio data buffered for guild {guild_id} "
                f"({len(pcm_data)} bytes)"
            )
        buf.extend(pcm_data)

    async def _audio_flush_loop(self, guild_id: int) -> None:
        """Periodically flush accumulated audio to the Gemini Live API.

        Runs every ``_FLUSH_INTERVAL_S`` seconds, draining whatever audio
        has accumulated in the send buffer.  This guarantees that speech
        tails (< one full buffer) are sent within 200ms.
        """
        send_count = 0
        total_bytes = 0
        idle_ticks = 0
        _SILENCE_TICKS = max(1, int(1.5 / self._FLUSH_INTERVAL_S))
        try:
            log_info(f"[live_session] Audio flush loop started for guild {guild_id}")
            while True:
                await asyncio.sleep(self._FLUSH_INTERVAL_S)

                state = self._sessions.get(guild_id)
                if not state or not state.is_active or not state._session:
                    log_info(
                        f"[live_session] Flush loop exiting: session inactive "
                        f"(guild {guild_id}, sent {send_count} chunks, "
                        f"{total_bytes} bytes total)"
                    )
                    break

                # Check if session needs reconnection
                if state.should_reconnect:
                    asyncio.create_task(self._reconnect(guild_id))
                    break

                buf = self._send_buffers.get(guild_id)
                if not buf:
                    idle_ticks += 1
                    # Discord stops sending packets when the user stops talking.
                    # After a silence timeout, send activity_end so Gemini knows
                    # the user finished speaking and should respond.
                    if idle_ticks == _SILENCE_TICKS and state._user_speaking:
                        try:
                            log_info(
                                f"[live_session] Sending activity_end for guild {guild_id} "
                                f"after {idle_ticks * self._FLUSH_INTERVAL_S:.1f}s silence"
                            )
                            await state._session.send_realtime_input(
                                activity_end=types.ActivityEnd()
                            )
                            state._user_speaking = False
                            log_live_send(guild_id, msg_type="activity_end")
                        except Exception as e:
                            log_warning(
                                f"[live_session] Failed to send activity_end for guild {guild_id}: {e}"
                            )
                    continue
                else:
                    chunk = bytes(buf)
                    buf.clear()

                    # Send activity_start on first audio after silence
                    if not state._user_speaking:
                        try:
                            log_info(
                                f"[live_session] Sending activity_start for guild {guild_id}"
                            )
                            await state._session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            state._user_speaking = True
                            log_live_send(guild_id, msg_type="activity_start")
                        except Exception as e:
                            log_warning(
                                f"[live_session] Failed to send activity_start for guild {guild_id}: {e}"
                            )

                    idle_ticks = 0

                send_count += 1
                total_bytes += len(chunk)

                # Log first send and then every 25 sends (~5s)
                if send_count == 1 or send_count % 25 == 0:
                    log_info(
                        f"[live_session] Flush #{send_count}: sending {len(chunk)} "
                        f"bytes to Gemini (guild {guild_id}, "
                        f"total {total_bytes} bytes)"
                    )

                try:
                    await state._session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type=LIVE_AUDIO_MIME)
                    )
                    if send_count == 1 or send_count % 25 == 0:
                        log_live_send(
                            guild_id,
                            msg_type="audio",
                            audio_bytes=len(chunk),
                            extra={"flush_num": send_count, "total_bytes": total_bytes},
                        )
                except Exception as e:
                    log_warning(
                        f"[live_session] Failed to send audio for guild {guild_id}: {e}"
                    )
                    if "close" in str(e).lower() or "1011" in str(e):
                        state.is_active = False
                        break
        except asyncio.CancelledError:
            log_debug(
                f"[live_session] Flush loop cancelled for guild {guild_id} "
                f"(sent {send_count} chunks)"
            )

    async def _history_sync_loop(self, guild_id: int) -> None:
        """Background task: periodically import text messages into live session.

        This is the "fallback" polling mechanism described in the design plan.
        It runs at ``self.history_sync_interval`` seconds, queries
        ``chat_history_cache.load_chat_history_for_guild`` for any messages newer
        than the last-seen timestamp, sends each one into Gemini via
        ``send_text`` and replicates it onto the ``discord_live_<guild>`` path
        so that the live prompt will see the text history as if it had been
        uttered in voice.
        """
        try:
            while True:
                await asyncio.sleep(self.history_sync_interval)

                state = self._sessions.get(guild_id)
                if not state or not state.is_active:
                    log_info(
                        f"[live_session] History sync loop exiting: session inactive (guild {guild_id})"
                    )
                    break

                try:
                    from core.chat_history_cache import (
                        load_chat_history_for_guild,
                        save_chat_message,
                    )

                    # Use the shared cursor from state so on-turn injection and
                    # the periodic sync loop never re-send the same messages.
                    new_msgs = await load_chat_history_for_guild(
                        guild_id, since=state.last_injected_ts
                    )
                    for msg in new_msgs:
                        text = msg.get("text") or ""
                        if text:
                            # Forward as a system-role context update so the
                            # model internalises the text without generating an
                            # audio response.  Using send_text (role=user,
                            # turn_complete=True) would trigger a model reply
                            # for every message, flooding the session.
                            sender = msg.get("sender_name") or "[unknown]"
                            await self.send_context_update(
                                guild_id,
                                f"[Text chat] {sender}: {text}",
                            )
                            # replicate to live history path
                            await save_chat_message(
                                interface_path=f"discord_live_{guild_id}",
                                message_text=text,
                                sender_name=msg.get("sender_name"),
                                sender_id=msg.get("sender_id"),
                                timestamp=msg.get("timestamp"),
                            )
                        # advance shared cursor regardless so we don't re-fetch
                        if msg.get("timestamp"):
                            state.last_injected_ts = msg["timestamp"]
                except Exception as e:
                    log_warning(
                        f"[live_session] History sync error for guild {guild_id}: {e}"
                    )
        except asyncio.CancelledError:
            log_debug(
                f"[live_session] History sync loop cancelled for guild {guild_id}"
            )

    async def send_context_update(self, guild_id: int, text: str) -> None:
        """Send or buffer a system-role context update message to a Live API session.

        If the session is currently in the middle of a model turn we cannot
        safely inject the text yet (it would be appended to the turn that has
        already been processed).  In that case we queue the update on
        ``state.pending_context_updates`` and it will be flushed by
        ``_receive_loop`` when the current turn completes.  Otherwise we send
        immediately with ``turn_complete=False`` so the text is merged into the
        next outgoing turn.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active or not state._session:
            return

        # buffer if model currently generating
        if getattr(state, "generating", False):
            state.pending_context_updates.append(text)
            log_debug(
                f"[live_session] Buffered context update for guild {guild_id}: {text[:60]}"
            )
            return

        try:
            full_text = f"[System Context Update] {text}"
            await state._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=full_text)],
                ),
                turn_complete=False,
            )
            log_live_send(
                guild_id,
                msg_type="context_update",
                content=full_text,
                extra={"turn_complete": False},
            )
            log_info(
                f"[live_session] Sent context update to guild {guild_id}: {text[:60]}"
            )
        except Exception as e:
            log_warning(
                f"[live_session] Failed to send context update for guild {guild_id}: {e}"
            )

    async def _flush_pending_updates(self, guild_id: int) -> None:
        """Internal helper: send and clear any buffered context updates.

        This is called by ``_receive_loop`` when a model turn completes, but it
        can also be invoked manually by tests.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active or not state._session:
            return
        if not state.pending_context_updates:
            return
        for upd in list(state.pending_context_updates):
            try:
                _upd_text = f"[System Context Update] {upd}"
                await state._session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=_upd_text)],
                    ),
                    turn_complete=False,
                )
                log_live_send(
                    guild_id,
                    msg_type="context_update_flush",
                    content=_upd_text,
                )
                log_info(
                    f"[live_session] Flushed buffered context update for guild {guild_id}: {upd[:60]}"
                )
            except Exception as e:
                log_warning(
                    f"[live_session] Failed flush context update for guild {guild_id}: {e}"
                )
        state.pending_context_updates.clear()

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
            log_live_send(
                guild_id,
                msg_type="send_text",
                content=text,
                extra={"turn_complete": True},
            )
        except Exception as e:
            log_warning(f"[live_session] Failed to send text for guild {guild_id}: {e}")

    async def send_multimodal_context(
        self,
        guild_id: int,
        text: str | None = None,
        file_data: bytes | None = None,
        mime_type: str = "application/octet-stream",
    ) -> bool:
        """Inject a multimodal context message (text + optional file) into a Live session.

        This allows sending documents, images, or other files alongside a text
        description so the model can discuss them during voice conversation.

        Args:
            guild_id: Target guild's Live session.
            text: Optional text description / instruction for the file.
            file_data: Raw file bytes (image, PDF, etc.).
            mime_type: MIME type of ``file_data``.

        Returns:
            True if the message was sent successfully.
        """
        state = self._sessions.get(guild_id)
        if not state or not state.is_active or not state._session:
            log_warning(
                f"[live_session] Cannot send multimodal context: no active session for guild {guild_id}"
            )
            return False

        parts: list[types.Part] = []
        if text:
            parts.append(types.Part(text=f"[Document Context] {text}"))
        if file_data:
            parts.append(
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_data))
            )

        if not parts:
            return False

        try:
            await state._session.send_client_content(
                turns=types.Content(role="user", parts=parts),
                turn_complete=False,
            )
            log_info(
                f"[live_session] Sent multimodal context to guild {guild_id}: "
                f"text={bool(text)}, file={bool(file_data)} ({mime_type})"
            )
            return True
        except Exception as e:
            log_warning(
                f"[live_session] Failed to send multimodal context for guild {guild_id}: {e}"
            )
            return False

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
            log_warning(
                f"[live_session] Receive loop aborted: no state/session for guild {guild_id}"
            )
            return

        log_info(f"[live_session] Receive loop started for guild {guild_id}")

        msg_count = 0
        turn_count = 0
        try:
            # receive() covers ONE complete model turn then exits.
            # Loop to keep receiving across all turns for the duration of the session.
            while state.is_active and state._session:
                turn_count += 1
                log_info(
                    f"[live_session] Waiting for turn {turn_count} from Gemini "
                    f"(guild {guild_id})"
                )
                # Per-turn transcript accumulators (filled from transcription fields).
                user_parts: list[str] = []
                model_parts: list[str] = []

                async for message in state._session.receive():
                    if not state.is_active:
                        break

                    msg_count += 1

                    # --- Deep debug: log every message's structure ---
                    sc = getattr(message, "server_content", None)
                    _tc = getattr(sc, "turn_complete", None) if sc else None
                    _interrupted = getattr(sc, "interrupted", None) if sc else None
                    _gen_complete = (
                        getattr(sc, "generation_complete", None) if sc else None
                    )
                    _has_model_turn = bool(
                        getattr(sc, "model_turn", None) if sc else None
                    )
                    _has_tool_call = bool(getattr(message, "tool_call", None))
                    _has_input_tx = bool(
                        getattr(sc, "input_transcription", None) if sc else None
                    )
                    _has_output_tx = bool(
                        getattr(sc, "output_transcription", None) if sc else None
                    )
                    log_debug(
                        f"[live_session] MSG#{msg_count} turn={turn_count} "
                        f"guild={guild_id}: "
                        f"model_turn={_has_model_turn} "
                        f"turn_complete={_tc} "
                        f"interrupted={_interrupted} "
                        f"gen_complete={_gen_complete} "
                        f"tool_call={_has_tool_call} "
                        f"input_tx={_has_input_tx} "
                        f"output_tx={_has_output_tx} "
                        f"has_data={bool(message.data)} "
                        f"has_text={bool(message.text)}"
                    )

                    # ── Bidirectional log to live_api.log ──
                    _input_tx_text: str | None = None
                    _output_tx_text: str | None = None
                    _tool_call_str: str | None = None
                    _sc = getattr(message, "server_content", None)
                    if _sc:
                        _it = getattr(_sc, "input_transcription", None)
                        if _it:
                            _input_tx_text = getattr(_it, "text", None)
                        _ot = getattr(_sc, "output_transcription", None)
                        if _ot:
                            _output_tx_text = getattr(_ot, "text", None)
                    _tc_obj = getattr(message, "tool_call", None)
                    if _tc_obj:
                        _fcs = getattr(_tc_obj, "function_calls", []) or []
                        _tool_call_str = ", ".join(
                            str(getattr(fc, "name", "?")) for fc in _fcs
                        )
                    log_live_recv(
                        guild_id,
                        turn=turn_count,
                        msg_num=msg_count,
                        model_turn=_has_model_turn,
                        turn_complete=_tc,
                        interrupted=_interrupted,
                        audio_bytes=len(message.data) if message.data else 0,
                        text=message.text,
                        input_transcript=_input_tx_text,
                        output_transcript=_output_tx_text,
                        tool_call=_tool_call_str,
                        extra={"gen_complete": _gen_complete}
                        if _gen_complete
                        else None,
                    )

                    if msg_count <= 3 or msg_count % 50 == 0:
                        log_info(
                            f"[live_session] Received message #{msg_count} "
                            f"(turn {turn_count}) from Gemini (guild {guild_id})"
                        )

                    # Audio — use SDK convenience property (handles inline_data decoding)
                    audio_data = message.data
                    if audio_data and self._on_audio:
                        log_debug(
                            f"[live_session] Received audio from model: "
                            f"{len(audio_data)} bytes"
                        )
                        try:
                            await self._on_audio(guild_id, audio_data)
                        except Exception as e:
                            log_warning(f"[live_session] Audio callback error: {e}")

                    # Text — use SDK convenience property (skips thought parts)
                    text = message.text
                    if text and self._on_text:
                        try:
                            await self._on_text(guild_id, text)
                        except Exception as e:
                            log_warning(f"[live_session] Text callback error: {e}")

                    # Collect audio transcription fragments for both sides of the turn.
                    # input_transcription = what the user said (speech-to-text).
                    # output_transcription = what the model said (transcription of audio output).
                    sc = getattr(message, "server_content", None)
                    if sc is not None:
                        it = getattr(sc, "input_transcription", None)
                        if it is not None:
                            t = getattr(it, "text", None)
                            if t:
                                # simply accumulate user transcription fragments
                                user_parts.append(t)
                        ot = getattr(sc, "output_transcription", None)
                        if ot is not None:
                            t = getattr(ot, "text", None)
                            if t:
                                model_parts.append(t)

                    # Tool/function calls
                    tool_call = getattr(message, "tool_call", None)
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
                                await state._session.send_tool_response(
                                    function_responses=types.FunctionResponse(
                                        name=fc_name,
                                        id=fc_id,
                                        response=result or {"status": "ok"},
                                    )
                                )
                            except Exception as e:
                                log_warning(
                                    f"[live_session] Tool call handling error: {e}"
                                )

                log_info(
                    f"[live_session] Turn {turn_count} receive loop exited "
                    f"(guild {guild_id}, {msg_count} total msgs)"
                )

                # Turn complete — fire callback with accumulated transcripts.
                if self._on_turn_complete and (user_parts or model_parts):
                    user_transcript = _clean_transcript(user_parts)
                    model_transcript = _clean_transcript(model_parts)
                    try:
                        await self._on_turn_complete(
                            guild_id, user_transcript, model_transcript
                        )
                    except Exception as e:
                        log_warning(f"[live_session] Turn complete callback error: {e}")

                # Model has finished generating; clear generating flag and flush
                # any buffered updates.
                state.generating = False
                await self._flush_pending_updates(guild_id)

                if not state.is_active:
                    break

                # After each turn, check if session is approaching time limit
                if state.should_reconnect:
                    log_info(
                        f"[live_session] Session nearing time limit for guild "
                        f"{guild_id}, reconnecting"
                    )
                    asyncio.create_task(self._reconnect(guild_id))
                    break

        except asyncio.CancelledError:
            log_debug(f"[live_session] Receive loop cancelled for guild {guild_id}")
        except Exception as e:
            log_error(f"[live_session] Receive loop error for guild {guild_id}: {e}")
            state.is_active = False
            log_info(f"[live_session] Attempting auto-reconnect for guild {guild_id}")
            asyncio.create_task(self._reconnect(guild_id))

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
            if not state:
                return  # Session was fully removed — nothing to reconnect

            log_info(
                f"[live_session] Reconnecting session for guild {guild_id} "
                f"(elapsed {state.elapsed_seconds:.0f}s, was_active={state.is_active})"
            )

            channel_id = state.channel_id
            _att_ctx = state.attachment_context
            await self.stop_session(guild_id)

            # Rebuild system instruction from current persona for the new session
            from core.prompt_engine import build_live_system_instruction

            instruction = await build_live_system_instruction(
                attachment_context=_att_ctx,
            )

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
                attachment_context=_att_ctx,
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
