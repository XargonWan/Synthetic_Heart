# interface/discord_interface.py (esempio)
"""Example Discord interface using the universal transport layer."""

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import List, Any

try:  # pragma: no cover - import may fail if dependency missing
    import discord  # type: ignore
except Exception:  # pragma: no cover - graceful fallback for tests without install
    discord = None

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.transport_layer import universal_send
from core.core_initializer import register_interface
from core.command_registry import execute_command
from core import message_queue
from plugins.chat_link import ChatLinkStore
from core.config_manager import config_registry


context_memory: dict[int, deque] = {}
chat_link_store = ChatLinkStore()


class DiscordInterface:
    """Discord interface mirroring Telegram bot behaviour."""
    
    display_name = "Discord Interface"

    def __init__(self, bot_token: str):
        self.bot_token = bot_token.strip() if bot_token else ""
        self.is_enabled = True
        self.disabled_reason = None
        
        # Register custom validation with the new validation system
        self._register_custom_validation()
        
        self.client = None
        if discord is not None:  # pragma: no branch
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents)

            @self.client.event
            async def on_ready():
                log_info(f"[discord_interface] Discord client ready as {self.client.user}")

            @self.client.event
            async def on_message(message):
                log_debug(f"[discord_interface] Raw message received: {message.content} from {message.author}")
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
                                    guild = b.get_guild(int(guild_id)) or await b.fetch_guild(int(guild_id))
                                except Exception as e:  # pragma: no cover
                                    log_warning(f"[discord_interface] guild name lookup failed: {e}")
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
            
            # Start message_queue consumer
            try:  # pragma: no cover - if no running loop
                asyncio.get_event_loop().create_task(message_queue.run())
            except Exception:
                pass
        else:
            reason = self.disabled_reason or "missing configuration"
            log_warning(f"[discord_interface] Interface loaded in disabled state: {reason}")
    
    def _disable(self, reason: str) -> None:
        """Mark interface as disabled with a reason."""
        self.is_enabled = False
        self.disabled_reason = reason

    async def start(self):
        """Start the Discord interface."""
        log_info("[discord_interface] Starting Discord interface...")
        
        # Update token from config (may have been loaded from DB after __init__)
        self.bot_token = get_discord_token()
        
        if not self.bot_token:
            self._disable("DISCORD_BOT_TOKEN not configured")
            log_warning("[discord_interface] Discord interface disabled: no token configured")
            return
        
        # Start the Discord client
        await self._start_discord_client()
        log_info("[discord_interface] Discord interface started successfully")

    async def _start_discord_client(self):
        """Start the Discord client with proper error handling."""
        if not self.bot_token or self.bot_token.strip() == "":
            log_warning("[discord_interface] No valid Discord bot token provided - skipping Discord startup")
            return
            
        try:
            log_info("[discord_interface] Starting Discord client...")
            await self.client.start(self.bot_token)
        except Exception as e:  # pragma: no cover - startup errors
            log_error(f"[discord_interface] Failed to start Discord client: {e}")
            if "Improper token" in str(e):
                log_warning("[discord_interface] Invalid Discord token - Discord interface will remain disabled")

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this interface."""
        return "discord_bot"

    @staticmethod
    def get_action_types() -> list[str]:
        """Return action types supported by this interface."""
        return ["message_discord_bot"]

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {
            "message_discord_bot": {
                "description": "Send a text message to a Discord channel.",
                "required_fields": ["text", "target"],
                "optional_fields": [],
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        if action_name == "message_discord_bot":
            return {
                "description": "Send a message to a Discord channel.",
                "payload": {
                    "text": {"type": "string", "example": "Hello Discord!", "description": "The message text to send."},
                    "target": {"type": "string", "example": "1234567890", "description": "The channel_id of the recipient."},
                    "reply_to_message_id": {"type": "integer", "example": 987654321, "description": "Optional ID of the message to reply to", "optional": True},
                },
            }
        return {}

    @staticmethod
    def validate_payload(action_type: str, payload: dict) -> list:
        """Validate payload for discord actions."""
        errors: list[str] = []

        if action_type != "message_discord_bot":
            return errors

        text = payload.get("text")
        if not isinstance(text, str) or not text:
            errors.append("payload.text must be a non-empty string")

        target = payload.get("target")
        if target is None:
            errors.append("payload.target is required")
        elif not isinstance(target, (int, str)):
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
        - send_message({"target": ..., "text": ...})
        """
        if isinstance(channel_id, dict):
            payload = channel_id
            text = payload.get("text", text)
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

        if channel_id is None or text is None:
            log_warning("[discord_interface] Missing channel_id or text in send_message")
            return

        try:
            await universal_send(self._discord_send, channel_id, text=text)
            log_debug(f"[discord_interface] Message sent to {channel_id}: {text}")
        except Exception as e:
            log_error(
                f"[discord_interface] Failed to send message to {channel_id}: {repr(e)}"
            )

    async def _discord_send(self, channel_id, text):
        """Internal Discord send method."""
        if self.client is None:  # pragma: no cover - safety
            raise RuntimeError("Discord client not initialized")
        channel = self.client.get_channel(int(channel_id))
        if channel is None:  # pragma: no cover - invalid channel
            raise RuntimeError("Unknown channel")
        await channel.send(text)

    async def _process_message(self, message):
        """Handle incoming Discord messages."""
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
                                SimpleNamespace(type="mention", offset=offset, length=len(mention_text))
                            )
                        break

            role_mentions_ids = []
            bot_role_ids = []
            if getattr(message, "role_mentions", None):
                for r in message.role_mentions:
                    role_mentions_ids.append(getattr(r, "id", None))
                    role_name = getattr(r, "name", "")
                    content = content.replace(f"<@&{getattr(r, 'id', '')}>", f"@{role_name}")
            if getattr(getattr(message, "guild", None), "me", None):
                bot_role_ids = [getattr(r, "id", None) for r in getattr(message.guild.me, "roles", [])]

            if not entities:
                entities = None

            # Simple ping check
            if content.lower() == "ping":
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

            # Track context memory
            channel_id = getattr(message.channel, "id", None)
            if channel_id is not None:
                history = context_memory.setdefault(channel_id, deque(maxlen=20))
                history.append(content)

            # Handle Discord replies
            reply_to = None
            ref = getattr(message, "reference", None)
            if ref is not None:
                replied = getattr(ref, "resolved", None)
                if replied is None and getattr(ref, "message_id", None):
                    try:  # pragma: no cover - network dependent
                        replied = await message.channel.fetch_message(ref.message_id)
                    except Exception as e:
                        log_debug(f"[discord_interface] Failed to fetch referenced message: {e}")
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
            
            if hasattr(message, 'channel') and message.channel:
                # In Discord.py, threads have type GUILD_PUBLIC_THREAD, GUILD_PRIVATE_THREAD, etc.
                channel_type = str(getattr(message.channel, 'type', ''))
                if '_thread' in channel_type.lower():
                    # We're in a thread - channel_id is already the thread ID
                    thread_id = channel_id  # Same as message.channel.id
                    parent_channel_id = getattr(message.channel, 'parent_id', None)
                    log_debug(f"[discord_interface] Message in thread: {thread_id}, parent: {parent_channel_id}")

            # Prepare simplified message for core queue  
            wrapped = SimpleNamespace(
                message_id=getattr(message, "id", None),
                chat_id=channel_id,  # In Discord, this is thread ID if in thread, channel ID otherwise
                text=content,
                caption=None,
                date=getattr(message, "created_at", None),
                thread_id=thread_id,  # Thread ID if in thread, None if in regular channel
                from_user=SimpleNamespace(
                    id=getattr(message.author, "id", None),
                    username=getattr(message.author, "name", None),
                    full_name=getattr(message.author, "display_name", getattr(message.author, "name", None)),
                ),
                chat=SimpleNamespace(
                    id=channel_id,
                    type="private" if getattr(message, "guild", None) is None else "group",
                    title=getattr(getattr(message, "channel", None), "name", None),
                    username=None,
                    first_name=None,
                    human_count=None,
                ),
                entities=entities,
                role_mentions=role_mentions_ids or None,
                bot_roles=bot_role_ids or None,
                reply_to_message=reply_to,
                attachments=getattr(message, 'attachments', [])  # Add attachments for image processing
            )

            try:
                await message_queue.enqueue(self.client, wrapped, context_memory, interface_id="discord_bot", original_message=message)
            except Exception as e:  # pragma: no cover - queue errors
                log_error(f"[discord_interface] message_queue enqueue failed: {e}")

        except Exception as e:  # pragma: no cover - unexpected errors
            log_error(f"[discord_interface] Error processing message: {e}")

    async def execute_action(
        self, action: dict, context: dict, bot: Any, original_message: object | None = None
    ) -> None:
        """Execute actions for this interface."""
        action_type = action.get("type")
        if action_type == "message_discord_bot":
            payload = action.get("payload", {})
            target = payload.get("target")
            text = payload.get("text")
            if text and target is not None:
                await self.send_message(target, text)

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
            log_info(f"[discord_interface] Successfully added reaction '{emoji}' to Discord message")
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
                target = payload.get("target")
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
                required_fields=["text", "target"],
                custom_validator=validate_discord_message,
                component_name="discord_interface"
            )
            
            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules("discord_interface", [rule])
            
            log_debug("[discord_interface] Registered custom validation rules with validation registry")
            
        except Exception as e:
            log_warning(f"[discord_interface] Failed to register custom validation: {e}")

# Expose class for dynamic loading
INTERFACE_CLASS = DiscordInterface

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


def get_discord_token() -> str:
    """Get the Discord bot token as a string."""
    return str(DISCORD_BOT_TOKEN) if DISCORD_BOT_TOKEN else ""


# Auto-register Discord interface at import time
# This ensures the interface is ALWAYS registered, even if disabled
log_info("[discord_interface] Creating Discord interface instance...")
discord_interface = DiscordInterface(get_discord_token())
log_info("[discord_interface] Discord interface instance created and registered")



