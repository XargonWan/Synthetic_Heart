import os
from collections import deque
from types import SimpleNamespace
from typing import List, Any

try:  # pragma: no cover - import may fail if dependency missing
    import discord  # type: ignore
    from discord import FFmpegPCMAudio
except Exception:  # pragma: no cover - graceful fallback for tests without install
    discord = None
    FFmpegPCMAudio = None

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.mention_utils import is_message_for_bot
from core.transport_layer import universal_send
from core.core_initializer import register_interface
from core.command_registry import execute_command
from core import message_queue
from plugins.chat_link import ChatLinkStore
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
from core.interfaces_registry import get_interface_registry


context_memory: dict[int, deque] = {}
chat_link_store = ChatLinkStore()


class DiscordInterface:
    """Discord interface mirroring Telegram bot behaviour."""

    display_name = "Discord Interface"

    chat_attention_state = {}

    def __init__(self, bot_token: str):
        # Ensure instance-level state is initialized immediately
        self.chat_attention_state = {}

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
        if discord is not None:  # pragma: no branch
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents)

            @self.client.event
            async def on_ready():
                log_info(
                    f"[discord_interface] Discord client ready as {self.client.user}"
                )

            @self.client.event
            async def on_message(message):
                log_debug(
                    f"[discord_interface] Raw message received: {message.content} from {message.author}"
                )
                await self._process_message(message)

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
        return ["message_discord_bot", "join_voice_discord", "leave_voice_discord", "audio_discord_bot"]

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
                "required_fields": ["channel_id"],
                "optional_fields": ["interface_path"],
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
                "description": "Join a Discord voice channel.",
                "payload": {
                    "channel_id": {
                        "type": "string",
                        "example": "123456789",
                        "description": "The ID of the voice channel to join.",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "discord_bot/1234567890/9876543210",
                        "description": "Optional interface path to derive guild/channel.",
                        "optional": True,
                    },
                },
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
            channel_id = payload.get("channel_id")
            interface_path = payload.get("interface_path")
            if not channel_id and not interface_path:
                errors.append("payload.channel_id or payload.interface_path is required")
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
                errors.append("payload.interface_path or payload.channel_id is required")
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

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute non-message actions."""
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "join_voice_discord":
            channel_id = payload.get("channel_id")
            interface_path = payload.get("interface_path")

            if not channel_id and interface_path:
                # Try to extract channel ID from interface path if not explicitly provided
                try:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    # discord_bot/guild_id/channel_id
                    if len(levels) >= 2:
                        channel_id = levels[1]
                except Exception:
                    pass

            if not channel_id:
                return {"status": "failed", "message": "Missing channel_id"}

            return await self._join_voice(channel_id)

        elif action_type == "leave_voice_discord":
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
                return {"status": "failed", "message": "Missing guild_id"}

            return await self._leave_voice(guild_id)

        elif action_type == "audio_discord_bot":
            audio_path = payload.get("audio")
            channel_id = payload.get("channel_id")
            interface_path = payload.get("interface_path")
            caption = payload.get("caption")

            if not channel_id and interface_path:
                try:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    # discord_bot/guild_id/channel_id
                    if len(levels) >= 2:
                        channel_id = levels[1]
                except Exception:
                    pass

            # Check if we are in a voice channel in this guild
            streaming = False
            if channel_id and self.client:
                try:
                    channel = self.client.get_channel(int(channel_id))
                    if not channel:
                        channel = await self.client.fetch_channel(int(channel_id))
                    
                    if channel and channel.guild and channel.guild.voice_client:
                        # We are connected to voice in this guild
                        # Verify if we are in the requested channel OR if the request was just for the guild context
                        # Actually, if we are in ANY voice channel in this guild, we can stream.
                        # But typically we want to stream to the channel we are in.
                        vc = channel.guild.voice_client
                        if vc.is_connected():
                            return await self._stream_audio(vc, audio_path)
                except Exception as e:
                    log_debug(f"[discord_interface] Voice check failed: {e}")

            # Fallback to sending as file
            log_debug("[discord_interface] Not in voice or lookup failed, sending as file attachment")
            await self.send_message(
                channel_id=channel_id,
                text=caption,
                audio=audio_path,
                interface_path=interface_path
            )
            return {"status": "success", "message": "Sent as file"}

        return {"status": "failed", "message": f"Unknown action {action_type}"}

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
        """Join a voice channel."""
        if not self.client:
            return {"status": "failed", "message": "Discord client not initialized"}

        try:
            channel = self.client.get_channel(int(channel_id))
            if not channel:
                channel = await self.client.fetch_channel(int(channel_id))

            if not channel:
                return {"status": "failed", "message": "Channel not found"}

            guild = channel.guild
            voice_client = guild.voice_client

            if voice_client:
                if voice_client.channel.id == channel.id:
                    return {"status": "success", "message": "Already in channel"}
                await voice_client.move_to(channel)
                return {"status": "success", "message": f"Moved to {channel.name}"}
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
        # Defensive check for state
        if not hasattr(self, "chat_attention_state"):
            self.chat_attention_state = {}

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
                try:
                    response = await execute_command(command, *args)
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
                )
            except Exception as e:
                log_warning(
                    f"[discord_interface] Failed to add message to context: {e}"
                )

            # Prepare simplified message for core queue
            # Detect wake/sleep commands early for flagging
            text_lower_check = content.lower()
            is_wake_sleep_cmd = (
                "hey 2b" in text_lower_check or "bye 2b" in text_lower_check
            )

            wrapped = SimpleNamespace(
                message_id=getattr(message, "id", None),
                chat_id=channel_id,  # In Discord, this is thread ID if in thread, channel ID otherwise
                interface_path=interface_path,  # Add interface_path to message
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

            # === Wake/Sleep & Attention Logic ===
            text_lower = content.lower()
            is_wake_command = "hey 2b" in text_lower
            is_sleep_command = "bye 2b" in text_lower

            chat_scope_id = thread_id if thread_id else channel_id
            if is_wake_command:
                self.chat_attention_state[chat_scope_id] = True
                log_debug(
                    f"[discord_interface] Wake command detected in chat {chat_scope_id}"
                )
            elif is_sleep_command:
                self.chat_attention_state[chat_scope_id] = False
                log_debug(
                    f"[discord_interface] Sleep command detected in chat {chat_scope_id}"
                )

            is_awake = self.chat_attention_state.get(chat_scope_id, False)
            is_explicit_trigger = is_wake_command or is_sleep_command
            if not is_explicit_trigger:
                if "@" in content:
                    is_explicit_trigger = True
                elif getattr(message, "guild", None) is None:
                    is_explicit_trigger = True
                elif reply_to and bot_user:
                    if getattr(reply_to.from_user, "id", None) == getattr(
                        bot_user, "id", None
                    ):
                        is_explicit_trigger = True

            directed, reason = await is_message_for_bot(wrapped, self.client)

            if is_awake:
                if not directed:
                    directed = True
                    reason = "awake_state"
            else:
                if directed and not is_explicit_trigger:
                    directed = False
                    reason = "asleep_state_no_trigger"
                    log_debug(
                        f"[discord_interface] Suppressed message due to Asleep state: {content}"
                    )
                elif not directed and is_explicit_trigger:
                    directed = True
                    reason = "explicit_trigger_asleep"

            if not directed:
                return

            try:
                await message_queue.enqueue(
                    self.client,
                    wrapped,
                    interface_id="discord_bot",
                    original_message=message,
                    skip_mention_check=True,
                )
            except Exception as e:  # pragma: no cover - queue errors
                log_error(f"[discord_interface] message_queue enqueue failed: {e}")

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


def _parse_trainer_id_from_config() -> int | None:
    """Extract trainer ID for discord_bot from TRAINER_IDS configuration."""
    trainer_ids = config_registry.get_var(
        "TRAINER_IDS",
        "",
        label="Trainer IDs",
        description="Comma-separated list of trainer IDs for each interface (format: interface_name:user_id)",
        group="core",
        component="discord_interface",
    )

    trainer_ids_str = str(trainer_ids) if trainer_ids else ""
    if not trainer_ids_str:
        return None

    for trainer_config in trainer_ids_str.split(","):
        trainer_config = trainer_config.strip()
        # Accept both 'discord_bot:' (primary) and 'discord:' (short alias)
        if trainer_config.startswith("discord_bot:"):
            try:
                return int(trainer_config.split(":")[1])
            except (ValueError, IndexError):
                log_warning(
                    f"[discord_interface] Invalid trainer ID format in TRAINER_IDS: {trainer_config}"
                )
                return None
        elif trainer_config.startswith("discord:"):
            try:
                return int(trainer_config.split(":")[1])
            except (ValueError, IndexError):
                log_warning(
                    f"[discord_interface] Invalid trainer ID format in TRAINER_IDS: {trainer_config}"
                )
                return None

    return None


def get_discord_token() -> str:
    """Get the Discord bot token as a string."""
    return str(DISCORD_BOT_TOKEN) if DISCORD_BOT_TOKEN else ""


# Auto-register Discord interface at import time
# This ensures the interface is ALWAYS registered, even if disabled
log_info("[discord_interface] Creating Discord interface instance...")
discord_interface = DiscordInterface(get_discord_token())
log_info("[discord_interface] Discord interface instance created and registered")
