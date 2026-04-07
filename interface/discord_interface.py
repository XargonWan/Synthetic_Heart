import os
import asyncio
import audioop
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import List, Any

try:  # pragma: no cover - import may fail if dependency missing
    import discord  # type: ignore
    from discord import FFmpegPCMAudio
except Exception:  # pragma: no cover - graceful fallback for tests without install
    discord = None
    FFmpegPCMAudio = None

# Optional: voice receive extension for listening to users in voice channels
try:
    from discord.ext import voice_recv  # type: ignore

    _HAS_VOICE_RECV = True
except ImportError:
    voice_recv = None  # type: ignore[assignment]
    _HAS_VOICE_RECV = False

import logging as _stdlib_logging

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.chat_attention import set_attention, evaluate_triggers

# ── Route discord.ext.voice_recv logs into the synth logger ──────────────
# voice_recv uses stdlib logging; without this its errors are invisible.
if _HAS_VOICE_RECV:
    _vr_logger = _stdlib_logging.getLogger("discord.ext.voice_recv")
    _synth_logger = _stdlib_logging.getLogger("synth")
    if _synth_logger.handlers and not _vr_logger.handlers:
        for _h in _synth_logger.handlers:
            _vr_logger.addHandler(_h)
        _vr_logger.setLevel(_stdlib_logging.DEBUG)

# ── Monkey-patch voice_recv internals to survive errors ──────────────────
# voice_recv's internal Opus decoder produces far better audio than our own
# (it has jitter buffer, FEC, PLC), but ANY unhandled exception in the
# PacketRouter thread kills it permanently — no more audio is received.
#
# We patch two things:
#   1. PacketDecoder._decode_packet — catch OpusError, return silence frame
#   2. PacketRouter._do_run — catch exceptions per-iteration so one bad
#      packet doesn't kill the whole thread
if _HAS_VOICE_RECV and discord is not None:
    from discord.ext.voice_recv.opus import PacketDecoder as _PacketDecoder
    from discord.ext.voice_recv.router import PacketRouter as _PacketRouter

    _vr_patch_log = _stdlib_logging.getLogger("discord.ext.voice_recv")

    # --- DAVE (Discord Audio Visual Encryption) support ---
    # voice_recv handles transport encryption (AEAD) but not DAVE E2EE.
    # After AEAD decryption, packet.decrypted_data may still be DAVE-encrypted.
    # We must DAVE-decrypt before Opus decode.
    try:
        import davey as _davey

        _DAVE_MEDIA_AUDIO = _davey.MediaType.audio
        _HAS_DAVEY = True
    except ImportError:
        _HAS_DAVEY = False

    # --- Patch 1: _decode_packet with DAVE decryption + resilience ---
    _orig_decode_packet = _PacketDecoder._decode_packet
    _SILENCE_FRAME = b"\x00" * 3840  # 20ms of 48kHz stereo 16-bit silence
    _decode_stats: dict[str, int] = {
        "count": 0,
        "errors": 0,
        "dave_ok": 0,
        "dave_fail": 0,
    }

    def _safe_decode_packet(
        self: "_PacketDecoder", packet: "Any"
    ) -> "tuple[Any, bytes]":
        _decode_stats["count"] += 1

        # --- DAVE decryption: unwrap E2EE before Opus decode ---
        if _HAS_DAVEY and packet and getattr(packet, "decrypted_data", None):
            dave_session = getattr(
                getattr(self.sink, "voice_client", None),
                "_connection",
                None,
            )
            dave_session = getattr(dave_session, "dave_session", None)
            user_id = self._cached_id
            if dave_session is not None and user_id is not None:
                try:
                    packet.decrypted_data = dave_session.decrypt(
                        user_id,
                        _DAVE_MEDIA_AUDIO,
                        bytes(packet.decrypted_data),
                    )
                    _decode_stats["dave_ok"] += 1
                except Exception as dave_exc:
                    _decode_stats["dave_fail"] += 1
                    if _decode_stats["dave_fail"] <= 10:
                        _vr_patch_log.warning(
                            "[voice_recv] DAVE decrypt failed: %s: %s",
                            type(dave_exc).__name__,
                            dave_exc,
                        )
                    # Fall through — Opus decode will likely fail, PLC handles it

        try:
            return _orig_decode_packet(self, packet)
        except Exception as exc:
            _decode_stats["errors"] += 1
            errs = _decode_stats["errors"]
            if errs <= 5 or errs % 500 == 0:
                _vr_patch_log.warning(
                    "[voice_recv] Opus decode error #%d: %s: %s",
                    errs,
                    type(exc).__name__,
                    exc,
                )
            # Use PLC instead of raw silence to maintain decoder state
            try:
                if self._decoder is not None:
                    return packet, self._decoder.decode(None, fec=False)
            except Exception:
                pass
            return packet, _SILENCE_FRAME

    _PacketDecoder._decode_packet = _safe_decode_packet  # type: ignore[assignment]

    # --- Patch 2: _do_run per-iteration resilience ---
    def _resilient_do_run(self: "_PacketRouter") -> None:  # type: ignore[name-defined]
        """Patched _do_run that catches exceptions per-iteration.

        The original exits on ANY exception, permanently killing audio
        receive.  This version logs and continues.
        """
        _consecutive_errors = 0
        while not self._end_thread.is_set():
            try:
                self.waiter.wait()
                with self._lock:
                    for decoder in self.waiter.items:
                        try:
                            data = decoder.pop_data()
                            if data is not None:
                                self.sink.write(data.source, data)
                        except Exception:
                            _consecutive_errors += 1
                            if (
                                _consecutive_errors <= 5
                                or _consecutive_errors % 100 == 0
                            ):
                                _vr_patch_log.warning(
                                    "[voice_recv] Error #%d in decode/write, continuing",
                                    _consecutive_errors,
                                    exc_info=True,
                                )
            except Exception:
                _consecutive_errors += 1
                if _consecutive_errors <= 5 or _consecutive_errors % 100 == 0:
                    _vr_patch_log.warning(
                        "[voice_recv] Error #%d in router loop, continuing",
                        _consecutive_errors,
                        exc_info=True,
                    )
            else:
                _consecutive_errors = 0

    _PacketRouter._do_run = _resilient_do_run  # type: ignore[assignment]
from core.transport_layer import universal_send
from core.core_initializer import register_interface
from core.command_registry import execute_command, handle_command_message, list_commands
from core import message_queue
from plugins.chat_link import ChatLinkStore
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
from core.interfaces_registry import get_interface_registry


context_memory: dict[int, deque] = {}
chat_link_store = ChatLinkStore()

# How often (turns) to flush a diary entry during a live voice session.
# Historically used by `_write_live_diary_entry`, now deprecated in favor of a
# single cumulative entry at session end.  The constant is left in place for
# backwards compatibility and may be removed in a future release.
_LIVE_DIARY_EVERY_N_TURNS: int = 1  # ignored


async def _write_live_diary_entry(
    guild_id: int, user_transcript: str, model_transcript: str
) -> None:
    """Write a diary entry for a completed Gemini Live voice turn.

    Uses run_in_executor so the sync add_diary_entry call doesn't block
    the event loop while aiomysql is scheduled back on it.

    **Deprecated.**  New code buffers turns and flushes a single entry at
    session end; this helper is preserved only for backwards compatibility
    tests.
    """
    try:
        import asyncio as _asyncio
        from plugins.ai_diary import add_diary_entry

        if user_transcript and len(user_transcript) > 80:
            summary = f"Voice turn: user said '{user_transcript[:80]}…'"
        elif user_transcript:
            summary = f"Voice turn: user said '{user_transcript}'"
        else:
            summary = "Voice turn (no user transcript)"

        loop = _asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: add_diary_entry(
                content=model_transcript,
                user_message=user_transcript,
                interaction_summary=summary,
                interface="discord",
                chat_id=str(guild_id),
            ),
        )
        log_debug(f"[live_voice] Diary entry written for guild {guild_id}")
    except Exception as e:
        log_warning(
            f"[live_voice] Failed to write diary entry for guild {guild_id}: {e}"
        )


async def _flush_live_diary(guild_id: int, buffer: list[tuple[str, str]]) -> None:
    """Flush buffered voice turns into a single diary entry.

    Runs in the event loop by default but dispatches the synchronous
    ``add_diary_entry`` call to an executor as before.

    ``buffer`` is a list of (user_transcript, model_transcript) tuples.
    """
    if not buffer:
        return
    try:
        import asyncio as _asyncio
        from plugins.ai_diary import add_diary_entry

        content = "\n\n".join(m for (_u, m) in buffer if m)
        user_msgs = "\n\n".join(u for (u, _m) in buffer if u)
        summary = f"Sessione vocale terminata – {len(buffer)} turni"
        loop = _asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: add_diary_entry(
                content=content,
                user_message=user_msgs,
                interaction_summary=summary,
                interface="discord",
                chat_id=str(guild_id),
            ),
        )
        log_debug(
            f"[live_voice] Flushed diary for guild {guild_id} ({len(buffer)} turns)"
        )
    except Exception as e:
        log_warning(
            f"[live_voice] Failed to flush live diary for guild {guild_id}: {e}"
        )


class DiscordInterface:
    """Discord interface mirroring Telegram bot behaviour."""

    display_name = "Discord Interface"

    # Chat attention state is centralized in core.chat_attention

    def __init__(self, bot_token: str):
        # Chat attention is centralized in core.chat_attention; no per-instance store required

        self.bot_token = bot_token.strip() if bot_token else ""
        self.is_enabled = True
        self.disabled_reason = None
        # Register custom validation with the new validation system
        self._register_custom_validation()

        # Register trainer ID if configured
        try:
            trainer_id = _parse_trainer_id_from_config()
            if trainer_id:
                get_interface_registry().set_trainer_id("discord_bot", trainer_id)
                log_info(f"[discord_interface] Registered trainer ID: {trainer_id}")
        except Exception as e:
            log_warning(f"[discord_interface] Failed to register trainer ID: {e}")

        self.client = None
        self._command_tree = None
        if discord is not None:  # pragma: no branch
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents)

            # Native Discord application command tree (registered slash commands)
            from discord import app_commands as _app_commands

            self._command_tree = _app_commands.CommandTree(self.client)

            # /leave [target] — registered as a real Discord application command so
            # Discord's slash-command picker recognises it and the interaction is
            # delivered even when the user picks it from the autocomplete UI.
            @self._command_tree.command(
                name="leave",
                description="Leave the current Discord voice channel",
            )
            @_app_commands.describe(
                target="Guild ID or channel name (omit when only in one channel)"
            )
            async def _app_leave(
                interaction: discord.Interaction, target: str = ""
            ) -> None:
                await interaction.response.defer(ephemeral=False)
                connections: list[tuple[Any, Any, str | None]] = [
                    (
                        g,
                        vc,
                        getattr(getattr(vc, "channel", None), "name", None),
                    )
                    for vc in getattr(self.client, "voice_clients", [])
                    if (g := getattr(vc, "guild", None)) is not None
                ]

                async def _do(g_obj: Any) -> dict:
                    await self._stop_live_voice(g_obj.id)
                    return await self._leave_voice(g_obj.id)

                async def _reply(text: str) -> None:
                    await interaction.followup.send(text)

                target = target.strip()
                if target:
                    matched = next(
                        (
                            g
                            for g, _vc, chan in connections
                            if str(g.id) == target
                            or (chan and chan.lower() == target.lower())
                        ),
                        None,
                    )
                    if not matched:
                        await _reply(f"❌ Not in a voice channel matching '{target}'.")
                        return
                    res = await _do(matched)
                    await _reply(
                        f"👋 Left voice channel in guild **{matched.name}**."
                        if res.get("status") == "success"
                        else f"❌ {res.get('message', 'Failed to leave.')}"
                    )
                    return

                if not connections:
                    await _reply("❌ I'm not in any voice channels.")
                    return
                if len(connections) == 1:
                    g_obj, _, _ = connections[0]
                    res = await _do(g_obj)
                    await _reply(
                        "👋 Left voice channel."
                        if res.get("status") == "success"
                        else f"❌ {res.get('message', 'Failed to leave.')}"
                    )
                    return

                lines = [
                    f"• **{g.name}** — #{chan or '?'} (ID: `{g.id}`)"
                    for g, _, chan in connections
                ]
                await _reply(
                    "I'm in multiple voice channels:\n"
                    + "\n".join(lines)
                    + "\n\nUse `/leave <guild_id>` to specify one."
                )

            # /join — join the voice channel of the user who issued the command.
            @self._command_tree.command(
                name="join",
                description="Move Synth to your current voice channel",
            )
            async def _app_join(interaction: discord.Interaction) -> None:
                await interaction.response.defer(ephemeral=False)

                # Resolve the voice channel the invoking user is in.
                voice_state = getattr(interaction.user, "voice", None)
                vc_channel = (
                    getattr(voice_state, "channel", None) if voice_state else None
                )
                if vc_channel is None:
                    await interaction.followup.send(
                        "❌ You're not in a voice channel. Join one first!"
                    )
                    return

                # If there is already a live session in a *different* channel,
                # stop it gracefully before moving.
                guild_id = interaction.guild_id
                existing_state = getattr(self, "_live_voice_state", {}).get(guild_id)
                if existing_state and existing_state.get("channel_id") != vc_channel.id:
                    await self._stop_live_voice(guild_id)

                # Delegate to the existing _start_live_voice path which handles
                # trainer detection, LiveSessionManager creation, audio sink, etc.
                result = await self._start_live_voice(vc_channel.id)
                if result.get("status") == "success":
                    await interaction.followup.send(
                        f"🎙️ Joined **{vc_channel.name}** and started live session."
                    )
                else:
                    # Live start failed — fall back to a plain voice join so the
                    # bot at least enters the channel for non-live usage.
                    err_msg = result.get("message", "")
                    try:
                        guild = interaction.guild
                        vc = guild.voice_client if guild else None
                        if vc:
                            await vc.move_to(vc_channel)
                        else:
                            await vc_channel.connect()
                        if err_msg:
                            await interaction.followup.send(
                                f"✅ Joined **{vc_channel.name}** "
                                f"(live session unavailable: {err_msg})"
                            )
                        else:
                            await interaction.followup.send(
                                f"✅ Joined **{vc_channel.name}**."
                            )
                    except Exception as _je:
                        await interaction.followup.send(
                            f"❌ Could not join **{vc_channel.name}**: {_je}"
                        )

            # ------------------------------------------------------------------
            # Dynamic registration: all core registry commands → Discord app cmds
            # ------------------------------------------------------------------
            def _make_core_cmd_handler(
                _name: str,
            ):
                """Factory: returns a Discord interaction handler for a core command.

                Uses ``handle_command_message`` (same path as Telegram) so that
                permission checks, routing and interface_context are applied
                consistently across interfaces.
                """

                async def _handler(
                    interaction: discord.Interaction, args: str = ""
                ) -> None:
                    await interaction.response.defer(ephemeral=False)
                    user_id = str(interaction.user.id)
                    # Build a Discord-compatible interface_context.
                    # Telegram-specific keys (update/context/bot) are absent;
                    # commands that only require interface_id / discord_interaction
                    # will work; Telegram-only commands will gracefully return an
                    # error rather than crash.
                    interface_ctx = {
                        "discord_interaction": interaction,
                        "discord_interface": self,
                        "interface_id": "discord_bot",
                        # Compat stubs so commands that probe for Telegram keys
                        # don't raise AttributeError
                        "update": None,
                        "context": None,
                        "bot": None,
                    }
                    cmd_text = f"/{_name} {args}".strip()
                    try:
                        response = await handle_command_message(
                            cmd_text, user_id, "discord_bot", interface_ctx
                        )
                        if response:
                            # Discord messages are capped at 2000 chars
                            await interaction.followup.send(response[:2000])
                    except Exception as _ce:
                        log_error(
                            f"[discord_interface] app_command /{_name} error: {_ce}"
                        )
                        await interaction.followup.send(
                            f"❌ Error executing `/{_name}`: {_ce}"
                        )

                # discord.py reads the callback name to derive the command name,
                # so we rename the inner function to match the command.
                _handler.__name__ = _name
                return _handler

            # Register every command from the core registry as a Discord app command.
            # /leave is already registered above as a custom command; skip it here.
            _SKIP_CMDS: set[str] = {"leave", "join", "help"}  # help has its own below

            for _cmd_name in list_commands():
                if _cmd_name in _SKIP_CMDS:
                    continue
                if len(_cmd_name) > 32:  # Discord app command name limit
                    continue
                try:
                    _core_handler = _make_core_cmd_handler(_cmd_name)
                    _app_cmd = _app_commands.Command(
                        name=_cmd_name,
                        description=f"SyntH: /{_cmd_name}",
                        callback=_core_handler,
                    )
                    self._command_tree.add_command(_app_cmd)
                except Exception as _reg_err:
                    log_warning(
                        f"[discord_interface] Failed to register app command '{_cmd_name}': {_reg_err}"
                    )

            # /help — explicitly registered so it shows a proper description
            @self._command_tree.command(
                name="help", description="Show available SyntH commands"
            )
            async def _app_help(interaction: discord.Interaction) -> None:
                await interaction.response.defer(ephemeral=False)
                try:
                    from core.command_registry import execute_command as _exec

                    response = await _exec("help")
                    if response:
                        await interaction.followup.send(response[:2000])
                except Exception as _he:
                    await interaction.followup.send(f"❌ {_he}")

            @self.client.event
            async def on_ready():
                log_info(
                    f"[discord_interface] Discord client ready as {self.client.user}"
                )
                # Step 1: copy global commands into each guild and sync per-guild.
                # Guild commands appear in the picker within seconds (no propagation delay).
                total_synced = 0
                for _guild in self.client.guilds:
                    try:
                        self._command_tree.copy_global_to(guild=_guild)
                        guild_synced = await self._command_tree.sync(guild=_guild)
                        total_synced += len(guild_synced)
                        log_info(
                            f"[discord_interface] Synced {len(guild_synced)} command(s) "
                            f"to guild '{_guild.name}' ({_guild.id})"
                        )
                    except Exception as _gs_err:
                        log_warning(
                            f"[discord_interface] Guild sync failed for {_guild.id}: {_gs_err}"
                        )
                log_info(
                    f"[discord_interface] Total: {total_synced} command slot(s) synced "
                    f"across {len(self.client.guilds)} guild(s)"
                )

                # Step 2: clear global commands from Discord's API so they don't
                # show alongside the guild-specific ones (which would cause duplicates).
                try:
                    self._command_tree.clear_commands(guild=None)
                    await self._command_tree.sync()
                    log_info(
                        "[discord_interface] Cleared global application commands (dedup)"
                    )
                except Exception as _clr_err:
                    log_warning(
                        f"[discord_interface] Failed to clear global commands: {_clr_err}"
                    )

            @self.client.event
            async def on_guild_join(guild: discord.Guild) -> None:
                """Sync commands immediately when invited to a new guild."""
                try:
                    self._command_tree.copy_global_to(guild=guild)
                    guild_synced = await self._command_tree.sync(guild=guild)
                    log_info(
                        f"[discord_interface] Joined guild '{guild.name}' ({guild.id}), "
                        f"synced {len(guild_synced)} command(s)"
                    )
                except Exception as _gj_err:
                    log_warning(
                        f"[discord_interface] Guild join sync failed for {guild.id}: {_gj_err}"
                    )

            @self.client.event
            async def on_message(message):
                log_debug(
                    f"[discord_interface] Raw message received: {message.content} from {message.author}"
                )
                await self._process_message(message)

            @self.client.event
            async def on_interaction(interaction: discord.Interaction) -> None:
                await self._command_tree.on_interaction(interaction)

            @self.client.event
            async def on_voice_state_update(member, before, after):
                await self._handle_voice_state_update(member, before, after)

            async def _resolver(guild_id, channel_id, bot_instance=None):
                b = bot_instance or self.client
                guild_name = None
                channel_name = None
                try:
                    if b:
                        channel = b.get_channel(int(channel_id))
                        if channel is None:
                            channel = await b.fetch_channel(int(channel_id))
                        if channel:
                            channel_name = getattr(channel, "name", None)
                            guild = getattr(channel, "guild", None)
                            if guild is None and guild_id is not None:
                                try:
                                    guild = b.get_guild(
                                        int(guild_id)
                                    ) or await b.fetch_guild(int(guild_id))
                                except Exception as e:  # pragma: no cover
                                    log_warning(
                                        f"[discord_interface] guild name lookup failed: {e}"
                                    )
                            if guild:
                                guild_name = getattr(guild, "name", None)
                except Exception as e:  # pragma: no cover
                    log_warning(f"[discord_interface] name lookup failed: {e}")
                return {"chat_name": guild_name, "message_thread_name": channel_name}

            ChatLinkStore.set_name_resolver("discord", _resolver)
        else:  # pragma: no cover - library not available
            self._disable("discord.py library not installed")

        # ALWAYS register, even if disabled
        register_interface("discord_bot", self)

        if self.is_enabled:
            log_info("[discord_interface] Discord interface registered and enabled")
            # Message queue consumer is started by main.py
        else:
            reason = self.disabled_reason or "missing configuration"
            log_warning(
                f"[discord_interface] Interface loaded in disabled state: {reason}"
            )

    def _disable(self, reason: str) -> None:
        """Mark interface as disabled with a reason."""
        self.is_enabled = False
        self.disabled_reason = reason

    def _register_trainer_id(self):
        """Register the trainer ID from config."""
        try:
            # Re-read config to ensure we have the latest value
            trainer_id = _parse_trainer_id_from_config()
            log_info(f"[discord_interface] Parsing TRAINER_IDS result: {trainer_id}")
            if trainer_id:
                get_interface_registry().set_trainer_id("discord_bot", trainer_id)
                log_info(
                    f"[discord_interface] Successfully registered trainer ID: {trainer_id}"
                )
            else:
                log_warning(
                    "[discord_interface] No trainer ID found for discord_bot in configuration"
                )
        except Exception as e:
            log_warning(f"[discord_interface] Failed to register trainer ID: {e}")

    async def start(self):
        """Start the Discord interface."""
        log_info("[discord_interface] Starting Discord interface...")

        # Ensure trainer ID is registered (retrying with fresh config)
        self._register_trainer_id()

        # Update token from config (may have been loaded from DB after __init__)
        self.bot_token = get_discord_token()

        if not self.bot_token:
            self._disable("DISCORD_BOT_TOKEN not configured")
            log_warning(
                "[discord_interface] Discord interface disabled: no token configured"
            )
            return

        # Start the Discord client
        await self._start_discord_client()
        log_info("[discord_interface] Discord interface started successfully")

    async def _start_discord_client(self):
        """Start the Discord client with proper error handling."""
        if not self.bot_token or self.bot_token.strip() == "":
            log_warning(
                "[discord_interface] No valid Discord bot token provided - skipping Discord startup"
            )
            return

        try:
            log_info("[discord_interface] Starting Discord client...")
            await self.client.start(self.bot_token)
        except Exception as e:  # pragma: no cover - startup errors
            log_error(f"[discord_interface] Failed to start Discord client: {e}")
            if "Improper token" in str(e):
                log_warning(
                    "[discord_interface] Invalid Discord token - Discord interface will remain disabled"
                )

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this interface."""
        return "discord_bot"

    @staticmethod
    def get_action_types() -> list[str]:
        """Return action types supported by this interface."""
        return [
            "message_discord_bot",
            "join_voice_discord",
            "leave_voice_discord",
            "audio_discord_bot",
        ]

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {
            "message_discord_bot": {
                "description": "Send a text message to a Discord channel.",
                # Prefer interface_path but accept legacy 'target' (validation handles either)
                "required_fields": ["text"],
                "optional_fields": ["interface_path", "target", "reply_to_message_id"],
            },
            "join_voice_discord": {
                "description": "Join a Discord voice channel.",
                "required_fields": [],
                "optional_fields": ["channel_id", "interface_path"],
            },
            "leave_voice_discord": {
                "description": "Leave the current Discord voice channel.",
                "required_fields": ["guild_id"],
                "optional_fields": ["interface_path"],
            },
            "audio_discord_bot": {
                "description": "Send audio to Discord. Streams if in voice, otherwise sends as file.",
                "required_fields": ["audio"],
                "optional_fields": ["interface_path", "channel_id", "caption"],
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        if action_name == "message_discord_bot":
            return {
                "description": "Send a message to a Discord channel.",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello Discord!",
                        "description": "The message text to send.",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "discord_bot/1234567890/9876543210",
                        "description": "REQUIRED. Interface path from input.payload.source.interface_path. Format: 'discord_bot/guild_id/channel_id' or 'discord_bot/guild_id/channel_id/thread_id' or 'discord_bot/user_id' for DM.",
                    },
                    "reply_to_message_id": {
                        "type": "integer",
                        "example": 987654321,
                        "description": "Optional ID of the message to reply to",
                        "optional": True,
                    },
                },
                "important_notes": [
                    "CRITICAL: ALWAYS use interface_path from input.payload.source.interface_path to reply in same conversation!",
                    "Never construct interface_path manually - use the exact value from input.payload.source.interface_path",
                ],
            }
        if action_name == "join_voice_discord":
            return {
                "description": "Join a Discord voice channel, optionally starting a live voice session.",
                "payload": {
                    "interface_path": {
                        "type": "string",
                        "example": "discord_bot/1234567890/9876543210",
                        "description": "REQUIRED. Use the exact interface_path from input.payload.source.interface_path.",
                    },
                },
                "important_notes": [
                    "IMPORTANT: Do NOT provide channel_id parameter - the system will automatically detect which voice channel the sender is in.",
                    "Simply provide interface_path only from input.payload.source.interface_path",
                ],
            }
        if action_name == "leave_voice_discord":
            return {
                "description": "Leave the current Discord voice channel in a guild.",
                "payload": {
                    "guild_id": {
                        "type": "string",
                        "example": "1234567890",
                        "description": "The ID of the guild to leave voice in.",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "discord_bot/1234567890/9876543210",
                        "description": "Optional interface path to derive guild.",
                        "optional": True,
                    },
                },
            }
        if action_name == "audio_discord_bot":
            return {
                "description": "Send audio to Discord. auto-streams if in Voice Channel, else sends file.",
                "payload": {
                    "audio": {
                        "type": "string",
                        "example": "/path/to/voice.ogg",
                        "description": "Path to the audio file.",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "discord_bot/1234567890/9876543210",
                        "description": "REQUIRED. Interface path to target.",
                    },
                    "caption": {
                        "type": "string",
                        "example": "Listen to this!",
                        "description": "Optional caption (for file messages only).",
                        "optional": True,
                    },
                },
            }
        return {}

    @staticmethod
    def validate_payload(action_type: str, payload: dict) -> list:
        """Validate payload for discord actions."""
        errors: list[str] = []

        if action_type == "join_voice_discord":
            # channel_id, interface_path, or author voice state auto-resolve at execute time
            return errors

        if action_type == "leave_voice_discord":
            guild_id = payload.get("guild_id")
            interface_path = payload.get("interface_path")
            if not guild_id and not interface_path:
                errors.append("payload.guild_id or payload.interface_path is required")
            return errors

        if action_type == "audio_discord_bot":
            if not payload.get("audio"):
                errors.append("payload.audio is required")
            if not payload.get("interface_path") and not payload.get("channel_id"):
                errors.append(
                    "payload.interface_path or payload.channel_id is required"
                )
            return errors

        if action_type != "message_discord_bot":
            return errors

        text = payload.get("text")
        if not isinstance(text, str) or not text:
            errors.append("payload.text must be a non-empty string")

        # Preferred routing uses interface_path; keep legacy support for payload.target
        interface_path = payload.get("interface_path")
        target = payload.get("target")

        if not interface_path and target is None:
            errors.append(
                "payload.interface_path is required (or payload.target for legacy)"
            )

        if interface_path is not None and not isinstance(interface_path, str):
            errors.append("payload.interface_path must be a string")

        if target is not None and not isinstance(target, (int, str)):
            errors.append("payload.target must be an int or string")

        reply_to = payload.get("reply_to_message_id")
        if reply_to is not None and not isinstance(reply_to, int):
            errors.append("payload.reply_to_message_id must be an int")

        return errors

    async def send_message(self, channel_id=None, text=None, **kwargs):
        """Send a message to a Discord channel.

        Supports multiple calling conventions:
        - send_message(channel_id, text)
        - send_message(chat_id=..., text=...)
        - send_message({"interface_path": ..., "text": ...})
        """
        audio_path = None
        if isinstance(channel_id, dict):
            payload = channel_id
            text = payload.get("text", text)
            interface_path = payload.get("interface_path")
            audio_path = payload.get("audio") or payload.get("audio_path")

            # Extract channel_id from interface_path if provided
            if interface_path:
                from core.interface_path_utils import parse_interface_path

                # For Discord: interface_path = discord_bot/guild_id/channel_id/thread_id
                # parse_interface_path returns (interface, [levels...])
                _, levels = parse_interface_path(interface_path)
                # We want the channel_id (level 2) or thread_id (level 3) if in thread
                if len(levels) >= 3:  # Has thread (guild, channel, thread)
                    channel_id = levels[2]  # thread_id if present
                elif len(levels) >= 2:  # No thread, guild/channel present
                    channel_id = levels[1]  # channel_id
                elif len(levels) >= 1:  # DM style: discord_bot/user_id
                    channel_id = levels[0]  # user_id
                log_debug(
                    f"[discord_interface] Extracted channel_id={channel_id} from interface_path"
                )
            else:
                # Fallback for backward compatibility
                channel_id = (
                    payload.get("target")
                    or payload.get("channel_id")
                    or payload.get("chat_id")
                )
        else:
            if channel_id is None:
                channel_id = (
                    kwargs.get("channel_id")
                    or kwargs.get("chat_id")
                    or kwargs.get("target")
                )
            if text is None:
                text = kwargs.get("text")

            audio_path = kwargs.get("audio") or kwargs.get("audio_path")

        if channel_id is None or (text is None and audio_path is None):
            log_warning(
                "[discord_interface] Missing channel_id or contents (text/audio) in send_message"
            )
            return

        try:
            reply_to = kwargs.get("reply_to_message_id")
            # Set temporary attribute so _discord_send can access reply id
            if reply_to is not None:
                setattr(self, "_last_reply_to_id", reply_to)

            await universal_send(
                self._discord_send,
                channel_id,
                text=text,
                reply_to_message_id=reply_to,
                audio_path=audio_path,
            )
            # Clear temporary attribute
            if hasattr(self, "_last_reply_to_id"):
                try:
                    delattr(self, "_last_reply_to_id")
                except Exception:
                    try:
                        del self._last_reply_to_id
                    except Exception:
                        pass

            # Save SyntH's response via core chat_context_manager
            try:
                from core.chat_context_manager import save_response_message
                from core.interface_path_utils import build_interface_path

                interface_path = build_interface_path("discord_bot", str(channel_id))
                await save_response_message(interface_path, text)
            except Exception as e:
                log_debug(
                    f"[discord_interface] Failed to save response via context_manager: {e}"
                )

            log_debug(
                f"[discord_interface] Message sent to {channel_id}: {text[:50] if text else '[Audio]'}"
            )
        except Exception as e:
            log_error(
                f"[discord_interface] Failed to send message to {channel_id}: {repr(e)}"
            )

    async def _stream_audio(self, voice_client, audio_path):
        """Stream audio to voice client."""
        if not os.path.exists(audio_path):
            return {"status": "failed", "message": "Audio file not found"}

        if voice_client.is_playing():
            voice_client.stop()

        try:
            # Requires FFmpeg installed
            source = FFmpegPCMAudio(audio_path)
            voice_client.play(source)
            log_info(f"[discord_interface] Streaming audio to voice: {audio_path}")
            return {"status": "success", "message": "Streaming started"}
        except Exception as e:
            log_error(f"[discord_interface] Streaming failed: {e}")
            return {"status": "failed", "message": str(e)}

    async def _join_voice(self, channel_id):
        """Join a voice channel.

        Uses VoiceRecvClient when available so that ``_start_live_voice``
        does not need to disconnect and reconnect.
        """
        if not self.client:
            return {"status": "failed", "message": "Discord client not initialized"}

        try:
            channel = self.client.get_channel(int(channel_id))
            if not channel:
                channel = await self.client.fetch_channel(int(channel_id))

            if not channel:
                return {"status": "failed", "message": "Channel not found"}

            # Guard: channel must be connectable (VoiceChannel or StageChannel).
            # TextChannel, CategoryChannel etc. cannot be connected to.
            if discord is not None and not isinstance(
                channel, (discord.VoiceChannel, discord.StageChannel)
            ):
                channel_type = type(channel).__name__
                log_warning(
                    f"[discord_interface] _join_voice: channel {channel_id} is a {channel_type}, "
                    "not a voice channel — refusing to connect"
                )
                return {
                    "status": "failed",
                    "message": f"Channel {channel_id} is not a voice channel (type: {channel_type})",
                }

            guild = channel.guild
            voice_client = guild.voice_client

            # Pick the right client class so live voice doesn't need to reconnect
            cls = voice_recv.VoiceRecvClient if _HAS_VOICE_RECV and voice_recv else None

            if voice_client:
                if voice_client.channel.id == channel.id:
                    return {"status": "success", "message": "Already in channel"}
                await voice_client.move_to(channel)
                return {"status": "success", "message": f"Moved to {channel.name}"}
            else:
                if cls:
                    await channel.connect(cls=cls)
                else:
                    await channel.connect()
                return {"status": "success", "message": f"Connected to {channel.name}"}

        except Exception as e:
            log_error(f"[discord_interface] Failed to join voice: {e}")
            return {"status": "failed", "message": str(e)}

    async def _leave_voice(self, guild_id):
        """Leave voice channel in a guild."""
        if not self.client:
            return {"status": "failed", "message": "Discord client not initialized"}

        try:
            guild = self.client.get_guild(int(guild_id))
            if not guild:
                guild = await self.client.fetch_guild(int(guild_id))

            if not guild:
                return {"status": "failed", "message": "Guild not found"}

            voice_client = guild.voice_client
            if voice_client:
                await voice_client.disconnect()
                return {"status": "success", "message": "Disconnected from voice"}
            else:
                return {"status": "success", "message": "Not in a voice channel"}
        except Exception as e:
            log_error(f"[discord_interface] Failed to leave voice: {e}")
            return {"status": "failed", "message": str(e)}

    # ------------------------------------------------------------------
    # Gemini Live API voice session management
    # ------------------------------------------------------------------

    async def _start_live_voice(
        self,
        channel_id: str | int,
        attachments: list[Any] | None = None,
    ) -> dict[str, str]:
        """Start a Gemini Live API voice session in a Discord voice channel.

        Joins the voice channel (using VoiceRecvClient if available for
        audio reception), starts a Live API WebSocket session with the
        current persona, and begins bidirectional audio streaming.

        Args:
            channel_id: The voice channel to join.
            attachments: Optional list of Discord attachment objects from the
                triggering message.  Text files are injected as context
                updates; images/PDFs use multimodal injection.
        """
        if not self.client:
            return {"status": "failed", "message": "Discord client not initialized"}

        if not _HAS_VOICE_RECV:
            return {
                "status": "failed",
                "message": "discord-ext-voice-recv not installed. Install with: uv add discord-ext-voice-recv",
            }

        try:
            channel = self.client.get_channel(int(channel_id))
            if not channel:
                channel = await self.client.fetch_channel(int(channel_id))
            if not channel:
                return {"status": "failed", "message": "Channel not found"}

            guild = channel.guild
            guild_id = guild.id

            # Resolve a live-capable engine from the cortex registry WITHOUT
            # touching the globally active cortex (which may be any engine that
            # does not support the Live API).  We look for the first registered
            # engine that exposes get_live_session_manager(), preferring the
            # configured LIVE_CORTEX engine and loading it on demand if needed.
            from core.cortex_registry import get_cortex_registry as _get_creg
            from core.config_manager import config_registry as _cfg_r

            _creg = _get_creg()
            _configured_live_engine: str = str(
                _cfg_r.get_value("LIVE_CORTEX", "") or ""
            ).strip()
            _all_engine_names = _creg.get_available_engines()
            # When the configured value is explicitly "disabled" treat as no
            # engine at all; we then leave _live_capable_engine = None below.
            if _configured_live_engine.lower() == "disabled":
                _configured_live_engine = ""
            # Prefer the configured LIVE_CORTEX; fall back to any already-loaded engine.
            _candidates = (
                [_configured_live_engine]
                if _configured_live_engine
                and _configured_live_engine in _all_engine_names
                else []
            ) + [e for e in _all_engine_names if e != _configured_live_engine]

            _live_capable_engine = None
            for _en in _candidates:
                _e = _creg.get_engine(_en)
                if _e is None and _en == _configured_live_engine:
                    # Load the configured LIVE_CORTEX engine on demand so we
                    # don't have to instantiate all registered engines just to
                    # find one with get_live_session_manager.
                    try:
                        _e = _creg.load_engine(_en)
                        log_info(
                            f"[live_voice] Loaded LIVE_CORTEX engine '{_en}' on demand"
                        )
                    except Exception as _le:
                        log_warning(
                            f"[live_voice] Could not load LIVE_CORTEX engine '{_en}': {_le}"
                        )
                        continue
                if _e and hasattr(_e, "get_live_session_manager"):
                    _live_capable_engine = _e
                    log_info(f"[live_voice] Using engine '{_en}' for Live API")
                    break

            if not _live_capable_engine:
                return {
                    "status": "failed",
                    "message": (
                        "No registered engine supports the Live API "
                        "(get_live_session_manager not found). "
                        "Enable a live-capable engine in the cortex settings."
                    ),
                }

            manager = _live_capable_engine.get_live_session_manager()
            if not manager:
                return {
                    "status": "failed",
                    "message": "Live session manager unavailable (check API key / SDK)",
                }

            # Connect to voice channel with VoiceRecvClient for audio reception
            vc = guild.voice_client
            if vc:
                if vc.channel.id != channel.id:
                    await vc.move_to(channel)
                # If already connected but not a VoiceRecvClient, reconnect
                if not isinstance(vc, voice_recv.VoiceRecvClient):
                    log_info(
                        f"[live_voice] Existing vc is {type(vc).__name__}, not VoiceRecvClient - reconnecting"
                    )
                    await vc.disconnect()
                    vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
            else:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

            log_info(
                f"[live_voice] Voice client connected: type={type(vc).__name__}, "
                f"is_connected={vc.is_connected()}, channel={vc.channel.name}"
            )

            # Extract text from attachments BEFORE building the system
            # instruction so document content is embedded in the core prompt
            # (survives reconnects and is strongly attended to by the model).
            _attachment_context_parts: list[str] = []
            if attachments:
                _DECODABLE_PREFIXES = (
                    "text/",
                    "application/json",
                    "application/xml",
                    "application/javascript",
                )
                _TEXT_EXTENSIONS = (
                    ".txt",
                    ".md",
                    ".csv",
                    ".log",
                    ".json",
                    ".xml",
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".ini",
                    ".cfg",
                    ".conf",
                    ".py",
                    ".js",
                    ".ts",
                    ".html",
                    ".css",
                    ".sh",
                    ".bat",
                    ".rst",
                    ".tex",
                )
                for att in attachments:
                    mime = getattr(att, "content_type", "") or ""
                    fname = getattr(att, "filename", "document")
                    is_decodable = any(mime.startswith(p) for p in _DECODABLE_PREFIXES)
                    if not is_decodable and fname:
                        is_decodable = any(
                            fname.lower().endswith(ext) for ext in _TEXT_EXTENSIONS
                        )
                    if not is_decodable:
                        log_info(
                            f"[live_voice] Skipping non-text attachment '{fname}' "
                            f"({mime}) — Live API only supports text context"
                        )
                        continue
                    try:
                        raw = await att.read()
                        doc_text = raw.decode("utf-8", errors="replace")
                        if len(doc_text) > 50000:
                            doc_text = doc_text[:50000] + "\n[... truncated]"
                        _attachment_context_parts.append(
                            f"=== Document: {fname} ===\n{doc_text}"
                        )
                        log_info(
                            f"[live_voice] Extracted text from '{fname}' "
                            f"({len(doc_text)} chars) for system instruction"
                        )
                    except Exception as _att_err:
                        log_warning(
                            f"[live_voice] Failed to read attachment '{fname}': {_att_err}"
                        )

            _attachment_context: str | None = (
                "\n\n".join(_attachment_context_parts)
                if _attachment_context_parts
                else None
            )

            # Build persona system instruction
            from core.prompt_engine import build_live_system_instruction

            system_instruction = await build_live_system_instruction(
                attachment_context=_attachment_context,
            )

            # Build Gemini function declarations from the SyntH action registry
            tools = _build_gemini_tool_declarations()

            # Set up audio output callback: Live API → Discord voice
            audio_buffer = LiveAudioBuffer()

            async def on_audio_from_model(gid: int, pcm_data: bytes) -> None:
                """Receive 24kHz mono PCM from Gemini, buffer for Discord playback."""
                audio_buffer.write(pcm_data)

            async def on_text_from_model(gid: int, text: str) -> None:
                """Log text responses from the live model (for debugging/diary)."""
                log_info(f"[live_voice] Model text for guild {gid}: {text[:200]}")

            async def on_tool_call(gid: int, call_dict: dict) -> dict:
                """Route Gemini function calls to the SyntH action pipeline."""
                return await _handle_live_tool_call(gid, call_dict, self.client)

            # Stable interface_path for this guild's live voice conversation.
            live_interface_path = f"discord_live_{guild_id}"

            async def on_turn_complete(
                gid: int, user_transcript: str, model_transcript: str
            ) -> None:
                """Store both sides of a live turn to history and buffer diary entries.

                This callback is invoked after every model turn.  We still persist the
                transcription to the chat history and semantic memories right away,
                but instead of writing a diary entry each turn we accumulate the
                transcripts in ``self._live_voice_state[gid]["diary_buffer"]`` and
                flush them when the session stops.
                """
                from core.chat_history_cache import save_chat_message

                # Resolve the real Discord user from the sink's last-speaker info.
                _sink = (
                    self._live_voice_state.get(gid, {}).get("sink")
                    if hasattr(self, "_live_voice_state")
                    else None
                )
                _speaker_name: str | None = getattr(_sink, "_last_speaker_name", None)
                _speaker_id: str | None = getattr(_sink, "_last_speaker_id", None)

                if user_transcript:
                    await save_chat_message(
                        interface_path=live_interface_path,
                        message_text=user_transcript,
                        sender_name=_speaker_name or "[voice_user]",
                        sender_id=_speaker_id or None,
                    )
                if model_transcript:
                    await save_chat_message(
                        interface_path=live_interface_path,
                        message_text=model_transcript,
                        sender_name="self",
                        sender_id="self",
                    )

                # Track turn count for diagnostics.
                live_state = (
                    self._live_voice_state.get(gid, {})
                    if hasattr(self, "_live_voice_state")
                    else {}
                )
                turn_n: int = live_state.get("turn_count", 0) + 1
                if hasattr(self, "_live_voice_state") and gid in self._live_voice_state:
                    self._live_voice_state[gid]["turn_count"] = turn_n
                    # append transcripts to buffer
                    buf = self._live_voice_state[gid].setdefault("diary_buffer", [])
                    buf.append((user_transcript, model_transcript))

                # Persist to the memories table so semantic memory search can
                # surface voice conversations in future context injections.
                if user_transcript and model_transcript:
                    from core.synth_core_memory import silently_record_memory

                    asyncio.create_task(
                        silently_record_memory(
                            user_text=user_transcript,
                            response_text=model_transcript,
                            tags='["voice", "auto"]',
                            scope="general",
                            source="voice",
                        )
                    )

                log_debug(
                    f"[live_voice] Turn {turn_n} stored for guild {gid}: "
                    f"user={len(user_transcript)} chars, "
                    f"model={len(model_transcript)} chars"
                )

            async def on_reconnect_failed(gid: int) -> None:
                """Called when all reconnect retries are exhausted.

                Cleans up the Discord voice state so the user isn't stuck
                with a zombie session that can never recover.
                """
                log_warning(
                    f"[live_voice] Reconnect failed for guild {gid} — "
                    "cleaning up voice state"
                )
                await self._stop_live_voice(gid)

            manager.set_audio_callback(on_audio_from_model)
            manager.set_text_callback(on_text_from_model)
            manager.set_tool_call_callback(on_tool_call)
            manager.set_turn_complete_callback(on_turn_complete)
            manager.set_reconnect_failed_callback(on_reconnect_failed)

            # Start the Live API session
            started = await manager.start_session(
                guild_id=guild_id,
                channel_id=channel.id,
                system_instruction=system_instruction,
                tools=tools,
                attachment_context=_attachment_context,
            )
            if not started:
                return {
                    "status": "failed",
                    "message": "Failed to start Live API session",
                }

            # Start playing audio from the Live API model to Discord
            source = LivePCMAudioSource(audio_buffer)
            if vc.is_playing():
                vc.stop()
            vc.play(source)

            # Start listening to Discord voice → forward to Live API
            sink = LiveVoiceAudioSink(
                manager, guild_id, self.client.loop, playback_buffer=audio_buffer
            )
            log_info(
                f"[live_voice] Starting vc.listen() with sink={sink}, "
                f"vc={vc}, vc.is_listening()={getattr(vc, 'is_listening', lambda: 'N/A')()}"
            )
            vc.listen(sink)
            log_info(
                f"[live_voice] vc.listen() completed, "
                f"vc.is_listening()={getattr(vc, 'is_listening', lambda: 'N/A')()}"
            )

            # Activate per-path cortex routing for the live voice interface
            # path so that any message-chain activity on this path uses the
            # live engine rather than the global cortex.
            #
            # NOTE: We intentionally do NOT pass a rejoin_callback here.
            # Session reconnection is handled at the Gemini API level by
            # LiveSessionManager._reconnect() which fires proactively via
            # should_reconnect (540s) and reactively on WebSocket errors.
            # A Discord-level rejoin would race with the Gemini reconnect
            # and cause cascading restarts.
            try:
                from cortex.live.live_base import LiveSessionManager as _LSM

                await _LSM.get_instance().activate_live_for_path(
                    live_interface_path, guild_id
                )
            except Exception as _ae:
                log_warning(f"[live_voice] activate_live_for_path failed: {_ae}")

            # Store state for cleanup
            if not hasattr(self, "_live_voice_state"):
                self._live_voice_state: dict[int, dict] = {}
            self._live_voice_state[guild_id] = {
                "channel_id": channel.id,
                "audio_buffer": audio_buffer,
                "sink": sink,
                "source": source,
                "turn_count": 0,
                "diary_buffer": [],
                "live_engine": _live_capable_engine,
                "interface_path": live_interface_path,
            }

            # Start a watchdog that re-creates the listening sink if the
            # PacketRouter thread dies unexpectedly.  The voice_recv library
            # tears down listening on *any* unhandled exception in its
            # background threads, so we need to be ready to restart.
            asyncio.create_task(
                self._listen_watchdog(guild_id, vc, manager),
                name=f"listen_watchdog_{guild_id}",
            )

            # Store attachment context for reconnects
            if _attachment_context:
                self._live_voice_state[guild_id]["attachment_context"] = (
                    _attachment_context
                )

            log_info(
                f"[discord_interface] Live voice session started in "
                f"guild {guild_id} channel {channel.name}"
            )
            return {
                "status": "success",
                "message": f"Live voice session started in {channel.name}",
            }

        except Exception as e:
            log_error(f"[discord_interface] Failed to start live voice: {e}")
            return {"status": "failed", "message": str(e)}

    async def _stop_live_voice(self, guild_id: int) -> dict[str, str]:
        """Stop the Gemini Live API voice session for a guild."""
        try:
            # Use the live engine stored at session start — NOT the global cortex,
            # which may be a different engine (e.g. selenium_gemini).
            _state_now = getattr(self, "_live_voice_state", {}).get(guild_id, {})
            _live_eng = _state_now.get("live_engine")
            if _live_eng and hasattr(_live_eng, "stop_live_voice_session"):
                await _live_eng.stop_live_voice_session(guild_id)

            # Stop listening and playing
            if self.client:
                guild = self.client.get_guild(guild_id)
                if guild and guild.voice_client:
                    vc = guild.voice_client
                    if hasattr(vc, "stop_listening"):
                        vc.stop_listening()
                    if vc.is_playing():
                        vc.stop()

            # Clean up state and deactivate live cortex routing
            if hasattr(self, "_live_voice_state"):
                state_backup = self._live_voice_state.pop(guild_id, None)

                # Flush any buffered diary turns before we drop the state
                if state_backup:
                    buf = state_backup.get("diary_buffer", [])
                    if buf:
                        await _flush_live_diary(guild_id, buf)

                if state_backup and state_backup.get("audio_buffer"):
                    state_backup["audio_buffer"].close()
                # Deactivate per-path cortex override via LiveSessionManager
                _ipath = state_backup.get("interface_path") if state_backup else None
                if _ipath:
                    try:
                        from cortex.live.live_base import LiveSessionManager

                        await (
                            LiveSessionManager.get_instance().deactivate_live_for_path(
                                _ipath, guild_id
                            )
                        )
                    except Exception as _de:
                        log_warning(
                            f"[discord_interface] deactivate_live_for_path failed: {_de}"
                        )

            log_info(
                f"[discord_interface] Live voice session stopped for guild {guild_id}"
            )
            return {"status": "success", "message": "Live voice session stopped"}

        except Exception as e:
            log_error(f"[discord_interface] Failed to stop live voice: {e}")
            return {"status": "failed", "message": str(e)}

    async def _listen_watchdog(
        self,
        guild_id: int,
        vc: Any,
        manager: Any,
    ) -> None:
        """Background task: monitor ``vc.is_listening()`` and restart if it drops.

        The voice_recv PacketRouter thread tears down listening on *any*
        unhandled exception (e.g. Opus decode error, threading race).  This
        watchdog detects the drop and re-creates the sink so audio reception
        continues.

        Args:
            guild_id: Guild whose voice session to monitor.
            vc: The VoiceRecvClient instance.
            manager: The LiveSessionManager forwarding audio to Gemini.
        """
        _POLL_INTERVAL = 2.0  # seconds between checks
        _restart_count = 0
        _MAX_RESTARTS = 10
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL)

                # Check if the live session is still active
                state = getattr(self, "_live_voice_state", {}).get(guild_id)
                if not state:
                    log_debug(
                        f"[listen_watchdog] Guild {guild_id} no longer has "
                        "live state — exiting watchdog"
                    )
                    return

                if not vc.is_connected():
                    log_debug(
                        f"[listen_watchdog] VC disconnected for guild "
                        f"{guild_id} — exiting watchdog"
                    )
                    return

                is_listening = getattr(vc, "is_listening", lambda: False)()
                if is_listening:
                    continue  # all good

                # Listening stopped unexpectedly — restart
                _restart_count += 1
                if _restart_count > _MAX_RESTARTS:
                    log_warning(
                        f"[listen_watchdog] Too many restarts ({_restart_count}) "
                        f"for guild {guild_id} — giving up"
                    )
                    return

                log_warning(
                    f"[listen_watchdog] Listening dropped for guild {guild_id} "
                    f"(restart #{_restart_count}), re-creating sink"
                )
                try:
                    new_sink = LiveVoiceAudioSink(manager, guild_id, self.client.loop)
                    vc.listen(new_sink)
                    state["sink"] = new_sink
                    log_info(
                        f"[listen_watchdog] Successfully restarted listening "
                        f"for guild {guild_id}"
                    )
                except Exception as e:
                    log_warning(
                        f"[listen_watchdog] Failed to restart listening "
                        f"for guild {guild_id}: {e}"
                    )
                    await asyncio.sleep(2.0)  # back off before next attempt
        except asyncio.CancelledError:
            log_debug(f"[listen_watchdog] Cancelled for guild {guild_id}")

    async def _handle_voice_state_update(
        self,
        member: Any,
        before: Any,
        after: Any,
    ) -> None:
        """Handle voice state changes — clean up Live API sessions on disconnect.

        Triggers cleanup when:
        - The bot itself is disconnected/kicked from voice.
        - All human users leave the voice channel the bot is in.
        """
        if not self.client:
            return

        bot_user = self.client.user
        if not bot_user:
            return

        guild = getattr(member, "guild", None)
        if not guild:
            return

        guild_id = guild.id

        # Check if we even have a live session for this guild
        if (
            not hasattr(self, "_live_voice_state")
            or guild_id not in self._live_voice_state
        ):
            return

        # Case 1: The bot itself left the voice channel
        if (
            member.id == bot_user.id
            and before.channel is not None
            and after.channel is None
        ):
            log_info(
                f"[discord_interface] Bot left voice in guild {guild_id}, "
                "cleaning up live session"
            )
            await self._stop_live_voice(guild_id)
            return

        # Case 2: The bot was moved to a different channel
        if (
            member.id == bot_user.id
            and before.channel is not None
            and after.channel is not None
            and before.channel.id != after.channel.id
        ):
            log_info(
                f"[discord_interface] Bot moved channels in guild {guild_id}, "
                "cleaning up live session"
            )
            await self._stop_live_voice(guild_id)
            return

        # Case 3: A user changed voice state (left/moved/muted/deafened). Check
        # whether the channel the bot was in is now empty of humans after the
        # change — but only count the member as "leaving" if they actually left
        # the channel (not just muted/deafened while staying in the same channel).
        if before.channel is not None:
            state = self._live_voice_state.get(guild_id)
            if state and state.get("channel_id") == before.channel.id:
                # Determine if the member actually left (or moved to a different
                # channel) versus only changing voice properties (mute/deafen).
                # ``after.channel`` of None means they disconnected entirely;
                # a different channel id means they moved.  Same or non-existent
                # after.channel with an equal id means a property-only change.
                _after_ch_id = getattr(after.channel, "id", None)
                actually_left = (
                    after.channel is None or _after_ch_id != before.channel.id
                )

                # Only subtract the member from the presence count when they
                # genuinely vacated the channel; property-only changes (mute,
                # deafen, stream start/stop) must not shrink the count.
                leaving_id: int | None = (
                    getattr(member, "id", None) if actually_left else None
                )
                human_members = [
                    m
                    for m in getattr(before.channel, "members", [])
                    if not getattr(m, "bot", False) and m.id != leaving_id
                ]

                log_debug(
                    f"[discord_interface] voice_state update in guild {guild_id}: "
                    f"member={getattr(member, 'id', None)}, "
                    f"actually_left={actually_left}, "
                    f"remaining humans={[m.id for m in human_members]}"
                )

                if not human_members:
                    log_info(
                        f"[discord_interface] All users left voice in guild {guild_id}, "
                        "auto-stopping live session"
                    )
                    await self._stop_live_voice(guild_id)

    async def _discord_send(
        self, channel_id, text, reply_to_message_id=None, audio_path=None
    ):
        """Internal Discord send method.

        This method is robust: it will try to resolve the provided numeric id
        as a channel first (server/channel/thread), and if not found it will
        attempt to resolve it as a user id and send a direct message (DM).
        """
        if self.client is None:  # pragma: no cover - safety
            raise RuntimeError("Discord client not initialized")

        # Try channel first
        try:
            channel = self.client.get_channel(int(channel_id))
            if channel is None:
                # Fallback to API fetch for uncached channels/threads
                try:
                    channel = await self.client.fetch_channel(int(channel_id))
                except Exception as e:  # pragma: no cover - network dependent
                    log_debug(
                        f"[discord_interface] fetch_channel failed for {channel_id}: {e}"
                    )
                    channel = None
        except Exception:
            channel = None

        if channel is None:
            # Fallback: try sending as a DM to a user id
            try:
                # Try quick local cache first
                user = None
                try:
                    user = self.client.get_user(int(channel_id))
                except Exception:
                    user = None

                if user is None:
                    # Last resort: fetch user from API
                    try:
                        user = await self.client.fetch_user(int(channel_id))
                    except Exception as e:  # pragma: no cover - network dependent
                        log_debug(
                            f"[discord_interface] fetch_user failed for {channel_id}: {e}"
                        )
                        user = None

                if user is not None:
                    log_debug(f"[discord_interface] Sending DM to user {channel_id}")

                    file_obj = None
                    if audio_path and os.path.exists(audio_path):
                        file_obj = discord.File(audio_path)

                    if text or file_obj:
                        await user.send(text or "", file=file_obj)
                    return
            except Exception as e:  # pragma: no cover - network dependent
                log_debug(
                    f"[discord_interface] DM send attempt failed for {channel_id}: {e}"
                )

            # If we reach here, both channel and user resolution failed
            raise RuntimeError(f"Unknown channel or user: {channel_id}")

        # Prepare file object
        file_obj = None
        if audio_path:
            if os.path.exists(audio_path):
                file_obj = discord.File(audio_path)
            else:
                log_warning(f"[discord_interface] Audio file not found: {audio_path}")

        # If reply to a specific message was requested, try to fetch and reply
        if reply_to_message_id:
            try:
                msg = await channel.fetch_message(int(reply_to_message_id))
                await msg.reply(text or "", file=file_obj)
                return
            except Exception as e:
                log_debug(
                    f"[discord_interface] Could not reply to message id {reply_to_message_id}: {e}"
                )

        await channel.send(text or "", file=file_obj)

    async def _process_message(self, message):
        """Handle incoming Discord messages."""
        # Chat attention is handled centrally via core.chat_attention; no per-instance state required

        try:
            if self.client and message.author == getattr(self.client, "user", None):
                return

            content = (message.content or "").strip()
            log_debug(
                f"[discord_interface] Received message in {getattr(message.channel, 'id', 'unknown')}: {content}"
            )

            if getattr(message, "guild", None):
                try:
                    await chat_link_store.update_names_from_resolver(
                        message.guild.id,
                        message.channel.id,
                        interface="discord",
                        bot=self.client,
                    )
                except Exception as e:  # pragma: no cover
                    log_warning(f"[discord_interface] update_names failed: {e}")

            bot_user = getattr(self.client, "user", None)
            entities = []
            if getattr(message, "mentions", None) and bot_user:
                for m in message.mentions:
                    if m.id == getattr(bot_user, "id", None):
                        mention_text = f"@{getattr(bot_user, 'name', '')}"
                        content = content.replace(f"<@{m.id}>", mention_text).replace(
                            f"<@!{m.id}>", mention_text
                        )
                        offset = content.find(mention_text)
                        if offset != -1:
                            entities.append(
                                SimpleNamespace(
                                    type="mention",
                                    offset=offset,
                                    length=len(mention_text),
                                )
                            )
                        break

            role_mentions_ids = []
            bot_role_ids = []
            if getattr(message, "role_mentions", None):
                for r in message.role_mentions:
                    role_mentions_ids.append(getattr(r, "id", None))
                    role_name = getattr(r, "name", "")
                    content = content.replace(
                        f"<@&{getattr(r, 'id', '')}>", f"@{role_name}"
                    )
            if getattr(getattr(message, "guild", None), "me", None):
                bot_role_ids = [
                    getattr(r, "id", None)
                    for r in getattr(message.guild.me, "roles", [])
                ]

            if not entities:
                entities = None

            # Simple ping check (strip mention for '@Bot ping')
            ping_check_content = content
            if bot_user and getattr(bot_user, "name", None):
                mention_text = f"@{bot_user.name}"
                ping_check_content = ping_check_content.replace(
                    mention_text, ""
                ).strip()

            if ping_check_content.lower() == "ping":
                await self._discord_send(message.channel.id, "pong")
                return

            # Slash-style command handling
            if content.startswith("/"):
                parts: List[str] = content[1:].split()
                if not parts:
                    return
                command, *args = parts

                # Discord-specific: /leave requires guild context not available to registry handlers
                if command == "leave":
                    guild = getattr(message, "guild", None)
                    if not guild:
                        await self._discord_send(
                            message.channel.id, "❌ Not in a server."
                        )
                        return

                    # gather all active voice clients; this is more reliable than
                    # iterating guilds because ``guild.voice_client`` can be None
                    # while ``client.voice_clients`` still contains a connection.
                    connections: list[tuple[Any, Any, str | None]] = []
                    if self.client:
                        for vc in getattr(self.client, "voice_clients", []):
                            g = getattr(vc, "guild", None)
                            chan_name = None
                            if hasattr(vc, "channel"):
                                chan_name = getattr(vc.channel, "name", None)
                            if g is not None:
                                connections.append((g, vc, chan_name))

                    # helper to perform leave on a specific guild object
                    async def _do_leave(g_obj):
                        await self._stop_live_voice(g_obj.id)
                        res = await self._leave_voice(g_obj.id)
                        return res

                    # if a specific target was provided, try to match it
                    if args:
                        target = args[0]
                        matched = None
                        for g_obj, vc, chan in connections:
                            if str(g_obj.id) == target or (
                                chan and chan.lower() == target.lower()
                            ):
                                matched = g_obj
                                break
                        if not matched:
                            await self._discord_send(
                                message.channel.id,
                                f"❌ I'm not in a voice channel matching '{target}'.",
                            )
                            return
                        leave_res = await _do_leave(matched)
                        if leave_res.get("status") == "success":
                            await self._discord_send(
                                message.channel.id,
                                f"👋 Left voice channel in guild {matched.name}.",
                            )
                        else:
                            await self._discord_send(
                                message.channel.id,
                                f"❌ {leave_res.get('message', 'Failed to leave.')}",
                            )
                        return

                    # no argument: act based on how many connections exist
                    if not connections:
                        await self._discord_send(
                            message.channel.id,
                            "❌ I'm not in any voice channels.",
                        )
                        return
                    if len(connections) == 1:
                        g_obj, _, _ = connections[0]
                        leave_res = await _do_leave(g_obj)
                        if leave_res.get("status") == "success":
                            await self._discord_send(
                                message.channel.id, "👋 Left voice channel."
                            )
                        else:
                            await self._discord_send(
                                message.channel.id,
                                f"❌ {leave_res.get('message', 'Failed to leave.')}",
                            )
                        return

                    # multiple connections: list them and instruct how to pick one
                    msg_list = "I'm currently connected to multiple voice channels:\n"
                    for g_obj, vc, chan in connections:
                        msg_list += f"• {g_obj.name} (id {g_obj.id}) in channel '{chan or 'unknown'}'\n"
                    msg_list += "Use `/leave <guild id or channel name>` to disconnect from one of them."
                    await self._discord_send(message.channel.id, msg_list)
                    return

                # Route through the centralized handler (same as Telegram).
                # This applies permission checks and passes interface_context.
                try:
                    interface_ctx = {
                        "discord_message": message,
                        "discord_interface": self,
                        "interface_id": "discord_bot",
                        # Compat stubs
                        "update": None,
                        "context": None,
                        "bot": None,
                    }
                    user_id = str(
                        getattr(getattr(message, "author", None), "id", "") or ""
                    )
                    response = await handle_command_message(
                        content, user_id, "discord_bot", interface_ctx
                    )
                    if response:
                        await self._discord_send(message.channel.id, response)
                except Exception as e:  # pragma: no cover - command errors
                    log_error(f"[discord_interface] Command {command} failed: {e}")
                return

            # Context tracking now handled via centralized chat_context_manager
            # (see interface_path generation below)
            channel_id = getattr(message.channel, "id", None)

            # Handle Discord replies
            reply_to = None
            ref = getattr(message, "reference", None)
            if ref is not None:
                replied = getattr(ref, "resolved", None)
                if replied is None and getattr(ref, "message_id", None):
                    try:  # pragma: no cover - network dependent
                        replied = await message.channel.fetch_message(ref.message_id)
                    except Exception as e:
                        log_debug(
                            f"[discord_interface] Failed to fetch referenced message: {e}"
                        )
                if replied is not None:
                    reply_to = SimpleNamespace(
                        message_id=getattr(replied, "id", None),
                        text=getattr(replied, "content", None),
                        caption=None,
                        date=getattr(replied, "created_at", None),
                        from_user=SimpleNamespace(
                            id=getattr(replied.author, "id", None),
                            username=getattr(replied.author, "name", None),
                            full_name=getattr(
                                replied.author,
                                "display_name",
                                getattr(replied.author, "name", None),
                            ),
                        ),
                    )

            # Check if author is currently in a voice channel (used for join_voice_discord auto-resolve)
            voice_channel_id: str | None = None
            try:
                author_voice = getattr(message.author, "voice", None)
                if author_voice and getattr(author_voice, "channel", None):
                    voice_channel_id = str(author_voice.channel.id)
                    log_debug(
                        f"[discord_interface] Author is in voice channel: {voice_channel_id}"
                    )
            except Exception:
                pass

            # Discord thread detection and handling
            thread_id = None
            parent_channel_id = None
            guild_id = str(message.guild.id) if message.guild else None

            if hasattr(message, "channel") and message.channel:
                # In Discord.py, threads have type GUILD_PUBLIC_THREAD, GUILD_PRIVATE_THREAD, etc.
                channel_type = str(getattr(message.channel, "type", ""))
                if "_thread" in channel_type.lower():
                    # We're in a thread - channel_id is already the thread ID
                    thread_id = channel_id  # Same as message.channel.id
                    parent_channel_id = getattr(message.channel, "parent_id", None)
                    log_debug(
                        f"[discord_interface] Message in thread: {thread_id}, parent: {parent_channel_id}"
                    )

            # Build interface_path for Discord
            from core.interface_path_utils import build_interface_path

            if guild_id:
                # Guild message: discord_bot/guild_id/channel_id/thread_id (if in thread)
                interface_path = build_interface_path(
                    "discord_bot",
                    guild_id,
                    str(channel_id),
                    str(thread_id) if thread_id else None,
                )
            else:
                # DM: discord_bot/user_id
                interface_path = build_interface_path(
                    "discord_bot", str(message.author.id)
                )
            log_debug(f"[discord_interface] Generated interface_path: {interface_path}")

            # Track context using centralized manager
            # NOTE: chat activity tracking is now centralized in chat_context_manager.add_message_to_context
            from core.chat_context_manager import add_message_to_context

            # Build reply metadata if this message is a reply to another message (best-effort)
            _reply_meta: dict | None = None
            _ref = getattr(message, "reference", None)
            if _ref is not None:
                _resolved = getattr(_ref, "resolved", None)
                if _resolved is not None:
                    _ref_author = getattr(_resolved, "author", None)
                    _reply_meta = {
                        "reply_to": {
                            "sender_name": (
                                getattr(_ref_author, "display_name", None)
                                or getattr(_ref_author, "name", None)
                                or "Unknown"
                            ),
                            "text": getattr(_resolved, "content", "") or "",
                            "message_id": getattr(_ref, "message_id", None),
                        }
                    }

            try:
                await add_message_to_context(
                    interface_path=interface_path,
                    message_text=content,
                    sender_name=message.author.display_name or message.author.name,
                    sender_id=str(message.author.id),
                    message_id=message.id,
                    timestamp=message.created_at.isoformat()
                    if hasattr(message.created_at, "isoformat")
                    else None,
                    metadata=_reply_meta,
                )
            except Exception as e:
                log_warning(
                    f"[discord_interface] Failed to add message to context: {e}"
                )

            # Prepare simplified message for core queue
            # Detect wake/sleep commands early for flagging
            text_lower_check = content.lower()
            _, _, is_wake_sleep_cmd = evaluate_triggers(text_lower_check)

            wrapped = SimpleNamespace(
                message_id=getattr(message, "id", None),
                chat_id=channel_id,  # In Discord, this is thread ID if in thread, channel ID otherwise
                interface_path=interface_path,  # Add interface_path to message
                voice_channel_id=voice_channel_id,  # Author's current voice channel, if any
                text=content,
                caption=None,
                date=getattr(message, "created_at", None),
                thread_id=thread_id,  # Thread ID if in thread, None if in regular channel
                is_wake_sleep_command=is_wake_sleep_cmd,  # Flag for prompt engine
                from_user=SimpleNamespace(
                    id=getattr(message.author, "id", None),
                    username=getattr(message.author, "name", None),
                    full_name=getattr(
                        message.author,
                        "display_name",
                        getattr(message.author, "name", None),
                    ),
                ),
                chat=SimpleNamespace(
                    id=channel_id,
                    type="private"
                    if getattr(message, "guild", None) is None
                    else "group",
                    title=getattr(getattr(message, "channel", None), "name", None),
                    username=None,
                    first_name=None,
                    human_count=None,
                ),
                entities=entities,
                role_mentions=role_mentions_ids or None,
                bot_roles=bot_role_ids or None,
                reply_to_message=reply_to,
                attachments=getattr(
                    message, "attachments", []
                ),  # Add attachments for image processing
            )

            # Attempt transcription of any audio attachment via Auris and
            # bypass normal pipeline if successful. The interface merely
            # writes the file and passes the text on; it does not notify
            # the user itself.
            try:
                from core.core_initializer import PLUGIN_REGISTRY
            except Exception:
                PLUGIN_REGISTRY = {}

            if PLUGIN_REGISTRY.get("auris_plugin"):
                for attachment in getattr(message, "attachments", []):
                    mime = getattr(attachment, "content_type", None)
                    if mime and mime.startswith("audio/"):
                        auris = PLUGIN_REGISTRY.get("auris_plugin")
                        try:
                            temp_dir = os.path.join(os.getcwd(), "tmp", "live_io")
                            os.makedirs(temp_dir, exist_ok=True)
                            ext = (
                                ".ogg"
                                if "ogg" in mime
                                else os.path.splitext(
                                    getattr(attachment, "filename", "")
                                )[-1]
                            )
                            if not ext:
                                ext = ".mp3"
                            path = os.path.join(
                                temp_dir,
                                f"disc_{getattr(message, 'id', 'x')}_{int(time.time())}{ext}",
                            )
                            data = await attachment.read()
                            with open(path, "wb") as f:
                                f.write(data)
                            log_debug(f"[discord_interface] wrote audio to {path}")
                            _auris_result = await auris.transcribe_audio(path, mime)
                            transcribed = _auris_result.text if _auris_result else None
                            if transcribed:
                                wrapped.text = transcribed
                                setattr(wrapped, "is_voice_input", True)
                                await message_queue.enqueue(
                                    self.client,
                                    wrapped,
                                    interface_id="discord_bot",
                                    original_message=message,
                                    skip_mention_check=True,
                                )
                                try:
                                    os.remove(path)
                                except Exception:
                                    pass
                                return
                        except Exception as e:
                            log_warning(
                                f"[discord_interface] Auris transcription error: {e}"
                            )
                        finally:
                            try:
                                if path and os.path.exists(path):
                                    os.remove(path)
                            except Exception:
                                pass

            # === Wake/Sleep & Attention Logic ===
            text_lower = content.lower()
            is_sleep_command, is_wake_command, _ = evaluate_triggers(text_lower)

            chat_scope_id = thread_id if thread_id else channel_id
            if is_wake_command:
                set_attention(chat_scope_id, True)
                log_debug(
                    f"[discord_interface] Wake command detected in chat {chat_scope_id}"
                )
            elif is_sleep_command:
                set_attention(chat_scope_id, False)
                log_debug(
                    f"[discord_interface] Sleep command detected in chat {chat_scope_id}"
                )

            # Default to awake (True) when not explicitly set
            is_explicit_trigger = is_wake_command or is_sleep_command
            if not is_explicit_trigger:
                # Treat as explicit trigger only for actual bot mentions, DMs, or replies to the bot.
                # DO NOT treat a plain '@' character as an explicit trigger.
                if entities:
                    # `entities` is populated earlier only when the bot was actually mentioned
                    is_explicit_trigger = any(
                        getattr(e, "type", "") == "mention" for e in entities
                    )
                elif getattr(message, "guild", None) is None:
                    # Direct messages always wake the bot
                    is_explicit_trigger = True
                elif reply_to and bot_user:
                    if getattr(reply_to.from_user, "id", None) == getattr(
                        bot_user, "id", None
                    ):
                        is_explicit_trigger = True

            # Attach explicit trigger flag so the core can enforce attention rules
            wrapped.is_explicit_trigger = is_explicit_trigger

            try:
                await message_queue.enqueue(
                    self.client,
                    wrapped,
                    interface_id="discord_bot",
                    original_message=message,
                    skip_mention_check=False,
                )
            except Exception as e:  # pragma: no cover - queue errors
                log_error(f"[discord_interface] message_queue enqueue failed: {e}")

            # --- live chat sync support ------------------------------------------------
            # When enabled and a live voice session is active for the guild, mirror
            # the textual message into the live context and replicate it on the
            # ``discord_live_<guild>`` history path.  This keeps the text and voice
            # stories aligned in real time.
            try:
                from core.config_manager import config_registry as _cfg
                from core.chat_history_cache import save_chat_message

                if (
                    _cfg.get_value("LIVE_SYNC_CHAT_HISTORY", True, value_type=bool)
                    and getattr(message, "guild", None)
                    and hasattr(self, "client")
                ):
                    guild_id = message.guild.id
                    # check whether a session is running
                    live_mgr = None
                    try:
                        from core.live_session_manager import LiveSessionManager

                        live_mgr = LiveSessionManager.get_instance()
                    except Exception:
                        live_mgr = None
                    if live_mgr and live_mgr.is_session_active(guild_id):
                        text = content

                        # --- inject text-decodable file attachments as context ---
                        # Live API only supports text; binary files (images,
                        # PDFs) cannot be sent as inline_data.
                        _DECODABLE_PREFIXES = (
                            "text/",
                            "application/json",
                            "application/xml",
                            "application/javascript",
                        )
                        _TEXT_EXTENSIONS = (
                            ".txt",
                            ".md",
                            ".csv",
                            ".log",
                            ".json",
                            ".xml",
                            ".yaml",
                            ".yml",
                            ".toml",
                            ".ini",
                            ".cfg",
                            ".conf",
                            ".py",
                            ".js",
                            ".ts",
                            ".html",
                            ".css",
                            ".sh",
                            ".bat",
                            ".rst",
                            ".tex",
                        )
                        for att in getattr(message, "attachments", []):
                            mime = getattr(att, "content_type", "") or ""
                            fname = getattr(att, "filename", "document")
                            _is_decodable = any(
                                mime.startswith(p) for p in _DECODABLE_PREFIXES
                            )
                            if not _is_decodable and fname:
                                _is_decodable = any(
                                    fname.lower().endswith(ext)
                                    for ext in _TEXT_EXTENSIONS
                                )
                            if not _is_decodable:
                                log_info(
                                    f"[discord_interface] Skipping non-text attachment "
                                    f"'{fname}' ({mime}) — Live API text-only"
                                )
                                continue
                            try:
                                raw = await att.read()
                                doc_text = raw.decode("utf-8", errors="replace")
                                if len(doc_text) > 50000:
                                    doc_text = doc_text[:50000] + "\n[... truncated]"
                                await live_mgr.send_context_update(
                                    guild_id,
                                    f"[Document: {fname}]\n{doc_text}",
                                )
                                log_info(
                                    f"[discord_interface] Injected text attachment "
                                    f"'{fname}' ({len(doc_text)} chars) into "
                                    f"live session for guild {guild_id}"
                                )
                            except Exception as _att_err:
                                log_warning(
                                    f"[discord_interface] Failed to inject "
                                    f"attachment into live session: {_att_err}"
                                )

                        # Forward into live model as a context update (system
                        # role, turn_complete=False) so the model internalises
                        # the text without generating an audio response for it.
                        sender = getattr(
                            message.author, "display_name", None
                        ) or getattr(message.author, "name", "unknown")
                        await live_mgr.send_context_update(
                            guild_id,
                            f"[Text chat] {sender}: {text}",
                        )
                        # replicate in history cache
                        await save_chat_message(
                            interface_path=f"discord_live_{guild_id}",
                            message_text=text,
                            sender_name=getattr(message.author, "name", None),
                            sender_id=str(getattr(message.author, "id", "")),
                        )
            except Exception as _sync_err:  # pragma: no cover - best effort
                log_debug(f"[discord_interface] live sync failed: {_sync_err}")

        except Exception as e:  # pragma: no cover - unexpected errors
            log_error(f"[discord_interface] Error processing message: {e}")

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: object | None = None,
    ) -> None:
        """Execute actions for this interface."""
        action_type = action.get("type")
        log_debug(
            f"[discord_interface] execute_action called with action_type={action_type}, payload={action.get('payload')}, context_keys={list(context.keys()) if context else []}"
        )
        if action_type == "message_discord_bot":
            payload = action.get("payload", {})
            target = payload.get("target")
            interface_path = payload.get("interface_path")
            text = payload.get("text")
            # Prefer interface_path (new style). If present, pass the whole payload
            if text and interface_path:
                try:
                    from core.persona_manager import PersonaManager

                    webui_session_id = context.get("webui_session_id") or context.get(
                        "session_id"
                    )
                    if webui_session_id:
                        persona_manager = PersonaManager.get_instance()
                        if persona_manager:
                            await persona_manager.set_animation_state(
                                "write", session_id=webui_session_id
                            )
                            log_debug(
                                f"[discord_interface] Set avatar animation to 'write' for WebUI session {webui_session_id}"
                            )
                except Exception as anim_exc:
                    log_debug(
                        f"[discord_interface] Could not set animation: {anim_exc}"
                    )

                log_debug(
                    f"[discord_interface] Sending message via interface_path={interface_path}"
                )
                await self.send_message(
                    {
                        "interface_path": interface_path,
                        "text": text,
                        "reply_to_message_id": payload.get("reply_to_message_id"),
                    }
                )
            elif text and target is not None:
                # Try to set animation to 'write' if WebUI context is available
                # Discord itself doesn't have session_id, but context might have it from WebUI
                try:
                    from core.persona_manager import PersonaManager

                    webui_session_id = context.get("webui_session_id") or context.get(
                        "session_id"
                    )
                    if webui_session_id:
                        persona_manager = PersonaManager.get_instance()
                        if persona_manager:
                            await persona_manager.set_animation_state(
                                "write", session_id=webui_session_id
                            )
                            log_debug(
                                f"[discord_interface] Set avatar animation to 'write' for WebUI session {webui_session_id}"
                            )
                except Exception as anim_exc:
                    log_debug(
                        f"[discord_interface] Could not set animation: {anim_exc}"
                    )

                log_debug(
                    f"[discord_interface] Sending message via legacy target={target}"
                )
                await self.send_message(target, text)
            else:
                log_warning(
                    f"[discord_interface] message_discord_bot called but no valid routing info (interface_path/target) found in payload: {payload}"
                )

        elif action_type == "join_voice_discord":
            payload = action.get("payload", {})
            channel_id = payload.get("channel_id")
            # note: start_live_voice flag removed – live sessions must be started
            # explicitly via a dedicated action (`start_live_voice_discord`).

            # Validate channel_id: if it looks like a guild ID instead of a channel ID,
            # clear it so auto-resolution can find the correct voice channel.
            if channel_id:
                interface_path = payload.get("interface_path") or context.get(
                    "interface_path"
                )
                if interface_path:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    if levels and str(channel_id) == str(levels[0]):
                        # channel_id matches guild_id - LLM likely confused them
                        log_warning(
                            f"[discord_interface] join_voice_discord: channel_id {channel_id} "
                            f"matches guild_id from interface_path - ignoring and using auto-resolve"
                        )
                        channel_id = None

            # Validate channel_id: if it resolves to a text channel, clear it so
            # auto-resolution picks the correct voice channel instead.
            if channel_id and self.client and discord is not None:
                try:
                    _ch = self.client.get_channel(int(channel_id))
                    if _ch is not None and isinstance(
                        _ch,
                        (
                            discord.TextChannel,
                            discord.ForumChannel,
                            discord.CategoryChannel,
                        ),
                    ):
                        log_warning(
                            f"[discord_interface] join_voice_discord: channel_id {channel_id} "
                            f"resolved to {type(_ch).__name__}, not a voice channel "
                            f"- ignoring and using auto-resolve"
                        )
                        channel_id = None
                except (ValueError, TypeError):
                    pass

            # Gate: LIVE_TRAINER_ONLY_VOICE — only the trainer may request a voice join
            try:
                from cortex.live.live_base import LiveSessionManager as _LSM

                _mgr = _LSM.get_instance()
                if _mgr.is_trainer_only_voice():
                    _sender_id: str | None = None
                    if original_message is not None:
                        _fu = getattr(original_message, "from_user", None)
                        _sender_id = str(getattr(_fu, "id", "") or "")
                    if not _sender_id:
                        _sender_id = str(context.get("sender_id", "") or "")

                    # determine whether the sender qualifies as trainer by
                    # numeric id or username(s) via the registry helper
                    registry = get_interface_registry()

                    def sender_is_trainer() -> bool:
                        if not _sender_id:
                            return False
                        if registry.is_trainer("discord_bot", _sender_id):
                            return True
                        # also try known name fields if a discord user object is
                        # available (original_message may carry it)
                        if original_message is not None:
                            user_obj = getattr(original_message, "from_user", None)
                            if user_obj:
                                names: list[str] = []
                                if getattr(user_obj, "name", None):
                                    names.append(str(user_obj.name))
                                if getattr(user_obj, "display_name", None):
                                    names.append(str(user_obj.display_name))
                                disc = getattr(user_obj, "discriminator", None)
                                if disc and names:
                                    names.append(f"{names[0]}#{disc}")
                                for n in names:
                                    if registry.is_trainer("discord_bot", n):
                                        return True
                        # context sender_name may also be useful
                        name_ctx = context.get("sender_name")
                        if name_ctx and registry.is_trainer("discord_bot", name_ctx):
                            return True
                        return False

                    if not sender_is_trainer():
                        log_warning(
                            "[discord_interface] join_voice_discord: denied — "
                            f"LIVE_TRAINER_ONLY_VOICE active and sender {_sender_id!r} "
                            f"is not recognised as a trainer"
                        )
                        return
            except Exception as _ge:
                log_debug(
                    f"[discord_interface] join_voice_discord: trainer gate check failed: {_ge}"
                )

            # Fallback: use the sender's current voice channel from the wrapped message
            # (original_message is the SimpleNamespace built in _process_message)
            if not channel_id and original_message is not None:
                vc_id = getattr(original_message, "voice_channel_id", None)
                if vc_id and self.client and discord is not None:
                    _vc_ch = self.client.get_channel(int(vc_id))
                    if _vc_ch is None or isinstance(
                        _vc_ch, (discord.VoiceChannel, discord.StageChannel)
                    ):
                        # Accept if it's a real voice channel OR if client can't resolve
                        # it right now (fetch will happen inside _join_voice instead)
                        channel_id = str(vc_id)
                        log_info(
                            f"[discord_interface] join_voice_discord: auto-resolved "
                            f"channel_id={channel_id} from sender's voice state"
                        )
                    else:
                        log_warning(
                            f"[discord_interface] join_voice_discord: voice_channel_id "
                            f"{vc_id} resolved to non-voice channel type "
                            f"{type(_vc_ch).__name__} — ignoring, will use guild lookup"
                        )
                elif vc_id:
                    # No client yet; trust the cached value
                    channel_id = str(vc_id)
                    log_info(
                        f"[discord_interface] join_voice_discord: auto-resolved "
                        f"channel_id={channel_id} from sender's voice state (no client validation)"
                    )

            # Last resort: look up the sender's voice state live via the guild
            if not channel_id and original_message is not None and self.client:
                try:
                    interface_path = payload.get("interface_path") or getattr(
                        original_message, "interface_path", None
                    )
                    if interface_path:
                        from core.interface_path_utils import parse_interface_path

                        _, levels = parse_interface_path(interface_path)
                        if levels:
                            guild = self.client.get_guild(int(levels[0]))
                            if guild:
                                sender_id = getattr(
                                    getattr(original_message, "from_user", None),
                                    "id",
                                    None,
                                )
                                if sender_id:
                                    member = guild.get_member(int(sender_id))
                                    if member and member.voice and member.voice.channel:
                                        channel_id = str(member.voice.channel.id)
                                        log_info(
                                            f"[discord_interface] join_voice_discord: resolved "
                                            f"channel_id={channel_id} from guild member voice state"
                                        )
                except Exception as e:
                    log_debug(
                        f"[discord_interface] join_voice_discord: guild voice lookup failed: {e}"
                    )

            if not channel_id:
                log_warning(
                    "[discord_interface] join_voice_discord: Missing channel_id and "
                    "sender is not in a voice channel"
                )
                return
            result = await self._join_voice(channel_id)

            # Propagate failure so run_actions can classify it correctly.
            # run_actions recognises {"error": ...} as a failed action.
            if result and result.get("status") == "failed":
                return {"error": result.get("message", "Voice join failed")}

            # Auto-start live session if the trainer is present in the channel.
            # Use voice_clients (always current) instead of get_channel() (cache-only).
            if self.client:
                try:
                    # Find the voice channel the bot just joined via voice_clients.
                    vc_channel = None
                    for _vc in getattr(self.client, "voice_clients", []):
                        _vc_ch = getattr(_vc, "channel", None)
                        if _vc_ch is None:
                            continue
                        # Match by channel_id if we resolved one, otherwise take first.
                        if channel_id and str(getattr(_vc_ch, "id", "")) != str(
                            channel_id
                        ):
                            continue
                        vc_channel = _vc_ch
                        break

                    if vc_channel is None and channel_id:
                        # Fallback: fetch from Discord API (bypasses cache)
                        try:
                            vc_channel = await self.client.fetch_channel(
                                int(channel_id)
                            )
                        except Exception as _fc_err:
                            log_warning(
                                f"[discord_interface] join_voice_discord: fetch_channel failed: {_fc_err}"
                            )

                    log_info(
                        f"[discord_interface] join_voice_discord: live auto-start check — "
                        f"vc_channel={getattr(vc_channel, 'id', None)}, "
                        f"members={[getattr(m, 'id', None) for m in getattr(vc_channel, 'members', [])]}"
                    )

                    if vc_channel is not None:
                        registry = get_interface_registry()
                        trainer_present = any(
                            not getattr(m, "bot", False)
                            and registry.is_trainer("discord_bot", str(m.id))
                            for m in getattr(vc_channel, "members", [])
                        )
                        if trainer_present:
                            log_info(
                                "[discord_interface] join_voice_discord: trainer detected "
                                f"in channel {getattr(vc_channel, 'id', channel_id)} — auto-starting live session"
                            )
                            live_result = await self._start_live_voice(
                                getattr(vc_channel, "id", channel_id),
                                attachments=getattr(
                                    original_message, "attachments", None
                                ),
                            )
                            if live_result and live_result.get("status") == "failed":
                                log_warning(
                                    "[discord_interface] join_voice_discord: live session "
                                    f"auto-start failed: {live_result.get('message')}"
                                )
                        else:
                            log_info(
                                "[discord_interface] join_voice_discord: no trainer in "
                                f"channel — skipping live session auto-start "
                                f"(members: {[getattr(m, 'id', None) for m in getattr(vc_channel, 'members', [])]}, "
                                f"trainer check uses 'discord_bot' interface)"
                            )
                    else:
                        log_warning(
                            "[discord_interface] join_voice_discord: could not resolve voice "
                            f"channel for live auto-start (channel_id={channel_id})"
                        )
                except Exception as _lve:
                    log_warning(
                        f"[discord_interface] join_voice_discord: live auto-start check failed: {_lve}"
                    )

            return result

        elif action_type == "leave_voice_discord":
            payload = action.get("payload", {})
            guild_id = payload.get("guild_id")
            interface_path = payload.get("interface_path")

            if not guild_id and interface_path:
                try:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    if len(levels) >= 1:
                        guild_id = levels[0]
                except Exception:
                    pass

            if not guild_id:
                log_warning("[discord_interface] leave_voice_discord: Missing guild_id")
                return
            await self._leave_voice(guild_id)

        elif action_type == "audio_discord_bot":
            payload = action.get("payload", {})
            audio_path = payload.get("audio")
            channel_id = payload.get("channel_id")
            interface_path = payload.get("interface_path")
            caption = payload.get("caption")

            if not channel_id and interface_path:
                try:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    if len(levels) >= 2:
                        channel_id = levels[1]
                except Exception:
                    pass

            if channel_id and self.client:
                try:
                    channel = self.client.get_channel(int(channel_id))
                    if not channel:
                        channel = await self.client.fetch_channel(int(channel_id))
                    if channel and channel.guild and channel.guild.voice_client:
                        vc = channel.guild.voice_client
                        if vc.is_connected():
                            await self._stream_audio(vc, audio_path)
                            return
                except Exception as e:
                    log_debug(f"[discord_interface] Voice check failed: {e}")

            log_debug(
                "[discord_interface] Not in voice or lookup failed, sending as file attachment"
            )
            await self.send_message(
                channel_id=channel_id,
                text=caption,
                audio=audio_path,
                interface_path=interface_path,
            )

        else:
            log_warning(
                f"[discord_interface] execute_action: unknown action_type={action_type}"
            )

    async def add_reaction(self, message, emoji: str) -> bool:
        """Add a reaction to a message.

        Args:
            message: The Discord message object
            emoji: The emoji to use as reaction

        Returns:
            bool: True if reaction was added successfully
        """
        try:
            await message.add_reaction(emoji)
            log_info(
                f"[discord_interface] Successfully added reaction '{emoji}' to Discord message"
            )
            return True
        except Exception as e:
            log_warning(f"[discord_interface] Failed to add reaction '{emoji}': {e}")
            return False

    async def handle_command(self, command_name: str, *args, **kwargs):
        """Process a slash command via the shared backend."""
        return await execute_command(command_name, *args, **kwargs)

    @staticmethod
    def get_interface_instructions():
        """Return specific instructions for Discord interface."""
        return (
            "DISCORD INTERFACE INSTRUCTIONS:\n"
            "- Use channel_id for targets.\n"
            "- Markdown is supported, but avoid advanced features not supported by Discord.\n"
            "- Messages sent to the same channel as the source will appear as replies when possible.\n"
            "- Use 'reply_message_id' to reply to specific messages.\n"
            "- Provide plain text or Markdown in the 'text' field.\n"
            "- Supports 'ping' and predefined codewords like the Telegram bot.\n"
            "- When a message arrives from Discord, respond using the message_discord_bot action; do not use other interfaces unless explicitly requested."
        )

    def _register_custom_validation(self):
        """Register custom validation rules with the new validation system."""
        try:
            from core.validation_registry import ValidationRule, get_validation_registry

            def validate_discord_message(payload):
                """Enhanced validation for Discord message actions."""
                errors = []

                # Validate text content
                text = payload.get("text")
                if text:
                    if len(text) > 2000:  # Discord message limit
                        errors.append("Message text cannot exceed 2000 characters")
                    if not text.strip():
                        errors.append("Message text cannot be empty or only whitespace")

                # Validate target (channel_id)
                # Validate routing info: prefer interface_path, accept legacy 'target'
                interface_path = payload.get("interface_path")
                target = payload.get("target")
                if not interface_path and target is None:
                    errors.append(
                        "Either payload.interface_path (preferred) or payload.target (legacy) is required"
                    )
                if interface_path is not None and not isinstance(interface_path, str):
                    errors.append("payload.interface_path must be a string")
                if target is not None:
                    if isinstance(target, str) and not target.isdigit():
                        errors.append("Channel ID must be numeric")
                    elif isinstance(target, int) and target <= 0:
                        errors.append("Channel ID must be positive")

                # Validate reply_to_message_id
                reply_to = payload.get("reply_to_message_id")
                if reply_to is not None:
                    if not isinstance(reply_to, int) or reply_to <= 0:
                        errors.append("reply_to_message_id must be a positive integer")

                return errors

            # Create custom validation rule
            rule = ValidationRule(
                action_type="message_discord_bot",
                # Require text only; custom_validator enforces that either interface_path or legacy 'target' is present
                required_fields=["text"],
                custom_validator=validate_discord_message,
                component_name="discord_interface",
            )

            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules("discord_interface", [rule])

            log_debug(
                "[discord_interface] Registered custom validation rules with validation registry"
            )

        except Exception as e:
            log_warning(
                f"[discord_interface] Failed to register custom validation: {e}"
            )


# ---------------------------------------------------------------------------
# Gemini Live API — tool/function calling bridge
# ---------------------------------------------------------------------------


def _build_gemini_tool_declarations() -> list[Any] | None:
    """Build Gemini function declarations from the SyntH action registry.

    Queries all plugins and interfaces via ``get_supported_actions()`` /
    ``get_prompt_instructions()`` and converts them into the
    ``google.genai.types.FunctionDeclaration`` format expected by the
    Gemini Live API ``tools`` parameter.

    Returns:
        A list containing a single ``types.Tool`` wrapping all function
        declarations, or ``None`` if no declarations could be built.
    """
    try:
        from google.genai import types as genai_types
    except ImportError:
        log_warning(
            "[live_voice] google-genai SDK unavailable, skipping tool declarations"
        )
        return None

    from core.action_parser import get_action_plugin_instructions

    instructions = get_action_plugin_instructions()
    if not instructions:
        return None

    declarations: list[Any] = []
    for action_name, instr in instructions.items():
        if not isinstance(instr, dict):
            continue

        # Build a JSON Schema-style properties dict from the payload spec
        payload_spec = instr.get("payload", {})
        properties: dict[str, Any] = {}
        required_fields: list[str] = []

        for field_name, field_meta in payload_spec.items():
            if not isinstance(field_meta, dict):
                continue
            prop: dict[str, str] = {
                "type": field_meta.get("type", "string").upper(),
            }
            desc = field_meta.get("description", "")
            if desc:
                prop["description"] = desc
            properties[field_name] = prop

            # Treat as required unless explicitly marked optional
            if not field_meta.get("optional", False):
                required_fields.append(field_name)

        description = instr.get("description", f"Execute {action_name} action")

        try:
            schema: dict[str, Any] = {
                "type": "OBJECT",
                "properties": properties,
            }
            if required_fields:
                schema["required"] = required_fields

            fd = genai_types.FunctionDeclaration(
                name=action_name,
                description=description,
                parameters=schema,
            )
            declarations.append(fd)
        except Exception as e:
            log_warning(
                f"[live_voice] Failed to build function declaration for {action_name}: {e}"
            )

    if not declarations:
        return None

    log_info(
        f"[live_voice] Built {len(declarations)} Gemini function declarations: "
        f"{[d.name for d in declarations]}"
    )
    return [genai_types.Tool(function_declarations=declarations)]


async def _handle_live_tool_call(
    guild_id: int,
    call_dict: dict[str, Any],
    bot: Any,
) -> dict[str, Any]:
    """Route a Gemini Live API function call to the SyntH action pipeline.

    Args:
        guild_id: The Discord guild the live session belongs to.
        call_dict: Dict with ``name``, ``id``, and ``args`` from the model.
        bot: The Discord client instance (passed as ``bot`` to ``run_action``).

    Returns:
        A result dict to send back to Gemini as the function response.
    """
    from core.action_parser import run_action

    action_name: str = call_dict.get("name", "")
    args: dict[str, Any] = call_dict.get("args", {}) or {}

    log_info(f"[live_voice] Tool call from guild {guild_id}: {action_name}({args})")

    action = {
        "type": action_name,
        "payload": args,
    }
    context: dict[str, Any] = {
        "source": "live_voice",
        "guild_id": guild_id,
    }

    try:
        result = await run_action(action, context, bot, None)
        if isinstance(result, dict):
            return result
        return {"status": "ok"}
    except Exception as e:
        log_error(f"[live_voice] Tool call execution failed: {e}")
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Gemini Live API audio processing classes
# ---------------------------------------------------------------------------

# Discord voice uses 48kHz stereo 16-bit PCM (960 samples/frame = 20ms).
# Gemini Live API expects 16kHz mono 16-bit PCM input and outputs 24kHz mono.
_DISCORD_RATE = 48000
_DISCORD_CHANNELS = 2
_DISCORD_SAMPLE_WIDTH = 2  # 16-bit
_GEMINI_INPUT_RATE = 16000
_GEMINI_OUTPUT_RATE = 24000
_DISCORD_FRAME_SIZE = 3840  # 20ms at 48kHz stereo 16-bit = 960*2*2


class LiveAudioBuffer:
    """Thread-safe buffer for PCM audio from Gemini → Discord.

    Receives 24kHz mono PCM from the Live API, resamples to 48kHz stereo
    for Discord voice playback.

    Both ``write`` (called from asyncio) and ``read`` (called from Discord's
    voice send thread) must be safe across threads — uses a threading lock.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._closed = False
        self._ratecv_state: Any = None  # audioop ratecv state for continuity
        self._last_nonempty_read: float = (
            0.0  # monotonic timestamp of last real audio read
        )

    def write(self, pcm_24k_mono: bytes) -> None:
        """Write 24kHz mono PCM data, converting to 48kHz stereo."""
        if self._closed or not pcm_24k_mono:
            return
        try:
            # 24kHz → 48kHz (ratio 2:1)
            upsampled, self._ratecv_state = audioop.ratecv(
                pcm_24k_mono,
                _DISCORD_SAMPLE_WIDTH,
                1,
                _GEMINI_OUTPUT_RATE,
                _DISCORD_RATE,
                self._ratecv_state,
            )
            # Mono → Stereo (duplicate channel)
            stereo = audioop.tostereo(upsampled, _DISCORD_SAMPLE_WIDTH, 1, 1)
            with self._lock:
                self._chunks.append(stereo)
        except Exception as e:
            log_warning(f"[live_audio_buffer] Resample/write failed: {e}")

    def read(self, nbytes: int) -> bytes:
        """Read up to nbytes of 48kHz stereo PCM for Discord playback."""
        with self._lock:
            if not self._chunks:
                return b""  # No data — signal silence to AudioSource
            # Join all chunks and split at the requested boundary
            data = b"".join(self._chunks)
            self._chunks.clear()

        self._last_nonempty_read = time.monotonic()
        if len(data) <= nbytes:
            return data
        # Return requested amount, put remainder back
        result = data[:nbytes]
        with self._lock:
            self._chunks.insert(0, data[nbytes:])
        return result

    def is_speaking(self, cooldown: float = 0.5) -> bool:
        """Return True if the buffer recently delivered real audio.

        Used by the voice sink to suppress echo — incoming audio is
        likely acoustic feedback while the bot is speaking.

        Args:
            cooldown: seconds after last real audio read to still
                      consider the bot "speaking" (accounts for
                      propagation delay and mic tail).
        """
        if self._chunks:
            return True
        return (time.monotonic() - self._last_nonempty_read) < cooldown

    def close(self) -> None:
        self._closed = True


class LivePCMAudioSource(discord.AudioSource if discord else object):  # type: ignore[misc]
    """Discord AudioSource that reads from a LiveAudioBuffer.

    Provides 20ms frames of 48kHz stereo 16-bit PCM to discord.py's
    voice client for playback.
    """

    def __init__(self, buffer: LiveAudioBuffer) -> None:
        self._buffer = buffer

    def read(self) -> bytes:
        """Return 20ms of audio (3840 bytes at 48kHz stereo 16-bit).

        Returns a full silence frame when the buffer is empty so Discord
        keeps the audio source alive (returning b'' would stop playback).
        """
        data = self._buffer.read(_DISCORD_FRAME_SIZE)
        if not data:
            # Return silence frame to keep the source alive without
            # showing the bot as "speaking"
            return b"\x00" * _DISCORD_FRAME_SIZE
        if len(data) < _DISCORD_FRAME_SIZE:
            # Pad partial frame with silence
            data += b"\x00" * (_DISCORD_FRAME_SIZE - len(data))
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        pass


if _HAS_VOICE_RECV and voice_recv is not None:

    class LiveVoiceAudioSink(voice_recv.AudioSink):  # type: ignore[misc]
        """AudioSink that forwards received Discord voice audio to Gemini Live API.

        Uses voice_recv's internal Opus decoder (with jitter buffer, FEC, and
        PLC) for high-quality PCM output.  The monkey-patched
        ``_decode_packet`` prevents ``OpusError`` from crashing the
        packet-router thread.

        NOTE: The ``write`` callback runs on the voice_recv packet-router thread,
        **not** the asyncio event loop thread.  We capture the running loop at
        init time and use ``call_soon_threadsafe`` to schedule coroutines.
        """

        def __init__(
            self,
            manager: Any,
            guild_id: int,
            loop: asyncio.AbstractEventLoop,
            playback_buffer: LiveAudioBuffer | None = None,
        ) -> None:
            super().__init__()
            self._manager = manager
            self._guild_id = guild_id
            # Use the long-lived discord client event loop so we don't grab a short-lived task loop
            self._loop: asyncio.AbstractEventLoop = loop
            self._packet_count: int = 0
            self._suppressed_count: int = 0
            self._ratecv_state: Any = None  # preserve resampler state across packets
            # Reference to the outgoing audio buffer for echo suppression.
            self._playback_buffer: LiveAudioBuffer | None = playback_buffer
            # Startup grace period: drop the first ~1s of audio while the
            # codec, jitter buffer, and DAVE key exchange settle.  At 50
            # packets/sec (20ms each) that's 50 packets ≈ 1 second.
            self._startup_grace: int = 50
            # Track the last real user who sent audio so on_turn_complete can
            # attribute the user-side transcript to the correct Discord account.
            self._last_speaker_name: str | None = None
            self._last_speaker_id: str | None = None

        def wants_opus(self) -> bool:
            """Return False so voice_recv decodes Opus internally.

            voice_recv's decoder has jitter buffer, FEC, and PLC for much
            better audio quality.  The monkey-patched ``_decode_packet``
            prevents OpusError from crashing the PacketRouter thread.
            """
            return False

        def write(self, user: Any, data: voice_recv.VoiceData) -> None:  # type: ignore[name-defined]
            """Process incoming audio from a Discord user.

            Called from the packet-router thread — must not call asyncio
            directly; instead schedules the coroutine on the captured loop.

            Args:
                user: The Discord user who spoke. May be None for unmapped SSRCs.
                data: VoiceData containing decoded 48kHz stereo PCM audio.
            """
            if user is None:
                return
            if getattr(user, "bot", False):
                return

            # Startup grace: drop early packets while codec/DAVE settle.
            if self._startup_grace > 0:
                self._startup_grace -= 1
                if self._startup_grace == 0:
                    log_info(
                        "[live_voice_sink] Startup grace period ended, forwarding audio"
                    )
                return

            # Echo suppression: drop incoming audio while the bot is
            # playing back Gemini responses (+ 0.5s cooldown).  The
            # user's mic picks up the bot's output and feeds it back,
            # creating an infinite conversation loop.
            if self._playback_buffer and self._playback_buffer.is_speaking():
                self._suppressed_count += 1
                if (
                    self._suppressed_count in (1, 100)
                    or self._suppressed_count % 1000 == 0
                ):
                    log_debug(
                        f"[live_voice_sink] Echo-suppressed {self._suppressed_count} packets"
                    )
                return

            if self._suppressed_count > 0:
                log_debug(
                    f"[live_voice_sink] Echo suppression ended after {self._suppressed_count} packets"
                )
                self._suppressed_count = 0

            pcm_48k_stereo = data.pcm
            if not pcm_48k_stereo:
                return

            # Update last-speaker info for transcript attribution.
            self._last_speaker_name = str(
                getattr(user, "display_name", None)
                or getattr(user, "name", None)
                or "[voice_user]"
            )
            self._last_speaker_id = str(getattr(user, "id", ""))

            self._packet_count += 1
            if self._packet_count == 1 or self._packet_count % 500 == 0:
                log_info(
                    f"[live_voice_sink] Received packet #{self._packet_count} "
                    f"from user {user}, {len(pcm_48k_stereo)} bytes PCM"
                )

            try:
                # Need at least one stereo sample (L+R = 4 bytes)
                if len(pcm_48k_stereo) < 4:
                    return

                # voice_recv Opus decoder ALWAYS outputs stereo
                # (CHANNELS=2), but frame duration varies (5-60ms),
                # so packet sizes range from 960 to 11520 bytes.
                # Extract left channel — speech lives there;
                # right channel may carry DC-offset decoder artifacts.
                mono = audioop.tomono(pcm_48k_stereo, _DISCORD_SAMPLE_WIDTH, 1, 0)
                rms_mono = audioop.rms(mono, _DISCORD_SAMPLE_WIDTH)

                if self._packet_count <= 10:
                    log_info(
                        f"[live_voice_sink] Audio pkt #{self._packet_count}: "
                        f"in={len(pcm_48k_stereo)}B "
                        f"mono={len(mono)}B rms={rms_mono}"
                    )

                # ── Diagnostics: save each pipeline stage to WAV ──
                if not hasattr(self, "_diag_stereo"):
                    import wave as _wave

                    self._diag_stereo = _wave.open("logs/voice_1_stereo.wav", "wb")
                    self._diag_stereo.setnchannels(2)
                    self._diag_stereo.setsampwidth(_DISCORD_SAMPLE_WIDTH)
                    self._diag_stereo.setframerate(_DISCORD_RATE)

                    self._diag_left = _wave.open("logs/voice_2_left.wav", "wb")
                    self._diag_left.setnchannels(1)
                    self._diag_left.setsampwidth(_DISCORD_SAMPLE_WIDTH)
                    self._diag_left.setframerate(_DISCORD_RATE)

                    self._diag_right = _wave.open("logs/voice_3_right.wav", "wb")
                    self._diag_right.setnchannels(1)
                    self._diag_right.setsampwidth(_DISCORD_SAMPLE_WIDTH)
                    self._diag_right.setframerate(_DISCORD_RATE)

                    self._diag_16k = _wave.open("logs/voice_4_16khz.wav", "wb")
                    self._diag_16k.setnchannels(1)
                    self._diag_16k.setsampwidth(_DISCORD_SAMPLE_WIDTH)
                    self._diag_16k.setframerate(_GEMINI_INPUT_RATE)

                    self._diag_count = 0
                    log_info(
                        "[live_voice_sink] Saving diagnostic WAVs to logs/voice_*.wav"
                    )

                if self._diag_stereo is not None:
                    self._diag_stereo.writeframes(pcm_48k_stereo)
                    self._diag_left.writeframes(mono)
                    right = audioop.tomono(pcm_48k_stereo, _DISCORD_SAMPLE_WIDTH, 0, 1)
                    self._diag_right.writeframes(right)

                if rms_mono == 0:
                    # Pure silence — don't waste bandwidth
                    return

                # Downsample 48kHz → 16kHz for Gemini Live API input.
                resampled, self._ratecv_state = audioop.ratecv(
                    mono,
                    _DISCORD_SAMPLE_WIDTH,
                    1,  # mono
                    _DISCORD_RATE,
                    _GEMINI_INPUT_RATE,
                    self._ratecv_state,
                )

                # Write resampled to diagnostic WAV
                if self._diag_stereo is not None:
                    self._diag_16k.writeframes(resampled)
                    self._diag_count += 1
                    if self._diag_count >= 500:
                        for f in (
                            self._diag_stereo,
                            self._diag_left,
                            self._diag_right,
                            self._diag_16k,
                        ):
                            f.close()
                        self._diag_stereo = None
                        log_info("[live_voice_sink] Diagnostic WAVs saved")

                asyncio.run_coroutine_threadsafe(
                    self._manager.send_audio(self._guild_id, resampled),
                    self._loop,
                )

            except Exception as e:
                log_warning(f"[live_voice_sink] Audio processing failed: {e}")

        def cleanup(self) -> None:
            """Clean up when sink is stopped."""
            import traceback

            log_warning(
                f"[live_voice_sink] Sink cleanup for guild {self._guild_id} "
                f"(packets received: {self._packet_count})\n"
                f"Call stack:\n{''.join(traceback.format_stack())}"
            )


else:
    # Stub when voice_recv is not installed
    LiveVoiceAudioSink = None  # type: ignore[assignment, misc]


# Expose class for dynamic loading
INTERFACE_CLASS = DiscordInterface

# Register exposed variable for WebUI
register_exposed_var(
    "DISCORD_BOT_TOKEN",
    label="Discord Bot Token",
    default="",
    value_type=str,
    ui_type="string",
    description="Bot token provided by the Discord developer portal.",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component="discord_bot",
)

# Instantiate and register the interface at import time so the core
# initializer can discover it during startup.
DISCORD_BOT_TOKEN = config_registry.get_var(
    "DISCORD_BOT_TOKEN",
    "",
    label="Discord Bot Token",
    description="Bot token provided by the Discord developer portal.",
    group="interface",
    component="discord_interface",
    sensitive=True,
)

discord_interface = None


def _parse_trainer_id_from_config() -> int | str | list[int | str] | None:
    """Extract trainer identifier(s) for ``discord_bot`` from the
    TRAINER_IDS configuration.

    The configuration value is a comma-separated list of ``interface:value``
    pairs.  For Discord we recognise either ``discord_bot`` or the short
    alias ``discord``.  The value component may be a numeric user ID (legacy)
    or a username (with optional ``#discriminator``).  Multiple entries are
    allowed; the function returns a list when more than one identifier is
    found, a scalar otherwise.
    """
    trainer_ids = config_registry.get_var(
        "TRAINER_IDS",
        "",
        label="Trainer IDs",
        description="Comma-separated list of trainer IDs for each interface (format: interface_name:user_id or interface_name:username)",
        group="core",
        component="discord_interface",
    )

    trainer_ids_str = str(trainer_ids) if trainer_ids else ""
    if not trainer_ids_str:
        return None

    results: list[int | str] = []
    for trainer_config in trainer_ids_str.split(","):
        trainer_config = trainer_config.strip()
        if not trainer_config:
            continue
        # Accept both 'discord_bot:' (primary) and 'discord:' (short alias)
        for prefix in ("discord_bot:", "discord:"):
            if trainer_config.startswith(prefix):
                value = trainer_config[len(prefix) :].strip()
                if not value:
                    continue
                # try to parse number but fall back to string
                try:
                    num = int(value)
                    results.append(num)
                except ValueError:
                    # leave username as-is
                    results.append(value)
                break
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    return results


def get_discord_token() -> str:
    """Get the Discord bot token as a string."""
    return str(DISCORD_BOT_TOKEN) if DISCORD_BOT_TOKEN else ""


# Auto-register Discord interface at import time
# This ensures the interface is ALWAYS registered, even if disabled
log_info("[discord_interface] Creating Discord interface instance...")
discord_interface = DiscordInterface(get_discord_token())
log_info("[discord_interface] Discord interface instance created and registered")
