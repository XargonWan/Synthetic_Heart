from core.logging_utils import log_debug

# Hardcoded fallback aliases for synth
synth_ALIASES = ["synth", "synthetic heart"]

# Pre-compute a lower-case version for faster checks
synth_ALIASES_LOWER = [alias.lower() for alias in synth_ALIASES]


def get_current_aliases() -> list[str]:
    """Return activation aliases merged from persona, config, and fallbacks."""
    aliases: list[str] = []

    # 1) Persona-derived aliases (if available)
    try:
        from core.persona_manager import get_persona_manager

        persona_manager = get_persona_manager()
        current_persona = persona_manager.get_current_persona()
        if current_persona:
            persona_aliases = getattr(current_persona, "aliases", None)
            if persona_aliases:
                log_debug(f"[mention] Loaded aliases from persona: {persona_aliases}")
                aliases.extend(list(persona_aliases))
            persona_name = getattr(current_persona, "name", None)
            if persona_name:
                aliases.append(persona_name)
    except Exception as e:
        log_debug(f"[mention] Error reading persona aliases: {e}")

    # 2) Config-derived aliases (DB/UI) as an additional source of truth
    try:
        from core.config_manager import config_registry

        raw = config_registry.get_value(
            "SYNTH_ALIASES",
            ["SyntH", "Synthetic Heart"],
            value_type="json",
        )
        if isinstance(raw, str):
            import json

            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    log_debug(
                        f"[mention] Loaded aliases from config (JSON parsed): {parsed}"
                    )
                    aliases.extend(parsed)
            except Exception:
                pass
        elif isinstance(raw, list):
            log_debug(f"[mention] Loaded aliases from config (direct list): {raw}")
            aliases.extend(raw)
    except Exception as config_e:
        log_debug(f"[mention] Error reading aliases from config registry: {config_e}")

    # 3) Always include hardcoded fallback aliases (e.g. 'synth')
    aliases.extend(synth_ALIASES)

    # De-duplicate while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if not alias:
            continue
        key = str(alias).strip()
        if not key:
            continue
        # Filter out single-character aliases (corrupted data from repr() serialization bug)
        if len(key) < 2:
            continue
        # Filter out punctuation-only aliases (artifacts of corrupted array storage)
        if key in ("[", "]", "'", '"', ",", " "):
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    if not deduped:
        log_debug(f"[mention] Using fallback aliases: {synth_ALIASES}")
        return synth_ALIASES

    return deduped


async def get_bot_username(bot):
    """Get the bot's username from the bot instance."""
    try:
        # Handle different bot shapes (Telegram, Discord, etc.)
        # 1) direct attribute (some wrappers expose .username)
        if hasattr(bot, "username"):
            return bot.username
        # 2) classic Telegram-like Bot with get_me()
        if hasattr(bot, "get_me"):
            me = await bot.get_me()
            return getattr(me, "username", None)
        # 3) discord.py Client/Cog exposes .user with .name and .id
        if hasattr(bot, "user") and getattr(bot, "user") is not None:
            user = getattr(bot, "user")
            return getattr(user, "name", None) or getattr(user, "username", None)
        return None
    except Exception as e:
        log_debug(f"[mention] Error getting bot username: {e}")
        return None


def is_synth_mentioned(text: str) -> bool:
    """Return ``True`` if ``text`` contains any alias for synth."""
    if not text:
        return False
    lowered = text.lower()
    aliases = get_current_aliases()
    # Match aliases as substrings intentionally: users expect 'synth'
    # to match 'synth-chan' and similar variations.
    for alias in aliases:
        if not alias:
            continue
        if alias.lower() in lowered:
            log_debug(f"[mention] synth alias matched (substring): '{alias}'")
            return True
    return False


def get_message_text(message) -> str | None:
    """
    Extract text from a message, checking both text and caption fields.
    Returns None if neither is available.
    """
    return (
        message.text
        if hasattr(message, "text") and message.text
        else message.caption
        if hasattr(message, "caption") and message.caption
        else None
    )


async def is_message_for_bot(
    message,
    bot,
    bot_username: str | None = None,
    human_count: int | None = None,
) -> tuple[bool, str | None]:
    """
    Check if a message is directed to the bot considering:
    - Explicit @mention of the bot
    - Reply to a message from the bot
    - Mention of synth aliases in the text
    - Private messages (always considered directed to bot)

    Args:
        message: Message object from the interface
        bot: Bot instance from the interface
        bot_username: Bot username (optional, will be detected if not provided)
        human_count: Number of human participants in the chat (excluding bots).
            If ``None``, interfaces are unable to provide this information
            and the bot will fall back to mention-based activation.

    Returns:
        tuple: (is_for_bot, reason)
            - is_for_bot: True if message is directed to the bot
            - reason: Optional string describing why a message was not
              considered for the bot. ``None`` when ``is_for_bot`` is True.
    """
    # First log to ensure function is called
    # Extract text from message (handles both text and caption)
    message_text = get_message_text(message)

    try:
        log_debug(
            f"[mention] ENTRY: Function called with message.text='{message_text}' chat_type='{getattr(message.chat, 'type', 'NO_CHAT_TYPE')}'"
        )
    except Exception as e:
        print(f"ERROR in log_debug: {e}")
        return False, "error_in_function"

    # If there's no textual content but the update contains media, we
    # previously treated it as automatically directed.  That caused the bot to
    # react/reply to *every* photo/voice/video in group chats even when it
    # wasn't mentioned, which was too noisy.  Instead only consider media-only
    # messages to be directed when they come from a private (1:1) chat.  Group
    # media will now fall through to the normal mention logic below.
    if not message_text:
        has_media = any(
            getattr(message, attr, None)
            for attr in (
                "photo",
                "voice",
                "video",
                "video_note",
                "document",
                "sticker",
                "animation",
                "audio",
            )
        )
        if has_media:
            chat_type = getattr(message.chat, "type", None)
            if chat_type == "private":
                log_debug(
                    "[mention] media-only private message detected; treating as directed"
                )
                return True, None
            else:
                log_debug("[mention] media-only group/channel message - not directed")
                # fall through so the message will be treated as not-for-bot
                # unless an alias/mention is present
                pass

    # Priority 1: Check for private messages (1:1 chat) - HIGHEST PRIORITY
    try:
        if message.chat.type == "private":
            log_debug("[mention] match_reason=private_message")
            log_debug(
                "[mention] ✅ Private message detected - PRIORITY 1 - always for bot"
            )
            return True, None
    except Exception as e:
        log_debug(f"[mention] Error checking private chat: {e}")
        return False, "error_checking_private"

    # Priority 1.5: Check chat awake/asleep state
    try:
        # Import locally to avoid circular import at module import time.
        from core.chat_attention import get_attention, evaluate_triggers

        chat_id = getattr(message.chat, "id", None)
        is_awake = get_attention(chat_id)
        if not is_awake:
            # If the chat is asleep, only wake commands should be considered.
            # evaluate_triggers returns (should_sleep, should_wake, is_wake_sleep_command)
            should_sleep, should_wake, _ = evaluate_triggers(
                message_text.lower() if message_text else ""
            )
            if not should_wake:
                # Log explicit, user-friendly message as requested.
                log_debug(
                    "SyntH is asleep, message is not a wake command so is not considered a message for bot"
                )
                return False, "chat_asleep"
            else:
                log_debug(
                    "[mention] Wake command detected in asleep chat - treating as message for bot"
                )
                return True, None
    except Exception as e:
        log_debug(f"[mention] Error checking chat attention: {e}")
        # Defer to normal behavior on errors checking attention.
        # Continue processing other checks rather than failing hard.

    # Priority 2: Check for reply to bot message
    if hasattr(message, "reply_to_message") and message.reply_to_message:
        reply_sender = getattr(message.reply_to_message, "from_user", None)
        if reply_sender:
            reply_username = getattr(reply_sender, "username", None)
            reply_id = getattr(reply_sender, "id", None)
            log_debug(
                f"[mention] Reply to message from: {reply_username} (ID: {reply_id})"
            )

            # Check if reply is to bot by username
            if (
                reply_username
                and bot_username
                and reply_username.lower() == bot_username.lower()
            ):
                log_debug("[mention] match_reason=reply_username")
                log_debug(
                    "[mention] ✅ Reply to bot message (username match) - PRIORITY 2 - message is for bot"
                )
                return True, None

            # Check if reply is to bot by ID
            # Support different bot shapes: bot.id or bot.user.id (discord.py)
            bot_id = None
            try:
                if hasattr(bot, "id"):
                    bot_id = getattr(bot, "id")
                elif hasattr(bot, "user") and getattr(bot, "user") is not None:
                    bot_id = getattr(bot.user, "id", None)
            except Exception:
                bot_id = None

            if reply_id and bot_id and reply_id == bot_id:
                log_debug("[mention] match_reason=reply_id")
                log_debug(
                    "[mention] ✅ Reply to bot message (ID match) - PRIORITY 2 - message is for bot"
                )
                return True, None

    # Priority 3: Check for @mention/tag (explicit mentions)
    if message_text and "@" in message_text:
        # Check for @synth mention
        if "@synth" in message_text.lower():
            log_debug("[mention] match_reason=@synth_mention")
            log_debug(
                "[mention] ✅ Explicit @synth mention found - PRIORITY 3 - message is for bot"
            )
            return True, None
        # Check for bot username if provided (case-insensitive)
        if bot_username:
            normalized_msg = message_text.lower()
            normalized_bot = f"@{bot_username}".lower()
            if normalized_bot in normalized_msg:
                log_debug(f"[mention] match_reason=@{bot_username}_mention")
                log_debug(
                    f"[mention] ✅ Explicit @mention found: @{bot_username} (case-insensitive) - PRIORITY 3 - message is for bot"
                )
                return True, None

    # Priority 4: Check for synth aliases in message text (activation words)
    if message_text:
        text_lower = message_text.lower()
        log_debug(f"[mention] Checking aliases in text: '{text_lower}'")
        aliases = get_current_aliases()
        log_debug(f"[mention] Current aliases to check: {aliases}")
        # Use the alias-aware helper which performs word-boundary-aware matching
        try:
            if is_synth_mentioned(message_text):
                log_debug("[mention] match_reason=alias")
                log_debug(
                    "[mention] ✅ Alias found in text - PRIORITY 4 - message is for bot"
                )
                return True, None
        except Exception:
            # Fallback to simple substring matching if helper unavailable for any reason
            for alias in aliases:
                if alias.lower() in text_lower:
                    log_debug(f"[mention] match_reason=alias:{alias}")
                    log_debug(
                        f"[mention] ✅ Alias found: '{alias}' - PRIORITY 4 - message is for bot"
                    )
                    return True, None
        log_debug(f"[mention] No aliases found in '{text_lower}'")

    # Priority 4b: Check for persona name in message text
    if message_text:
        try:
            log_debug("[mention] Checking persona via get_persona_manager()")
            from core.persona_manager import get_persona_manager

            persona_manager = get_persona_manager()
            log_debug(f"[mention] persona_manager instance: {persona_manager}")
            current_persona = persona_manager.get_current_persona()
            log_debug(f"[mention] current_persona: {current_persona}")
            if current_persona and current_persona.name:
                persona_name = current_persona.name
                text_lower = message_text.lower()
                # Use word-boundary-aware matching for persona name to avoid
                # accidental matches inside longer words (e.g. 'synthesis').
                import re

                pattern = r"\b" + re.escape(persona_name.lower()) + r"\b"
                try:
                    if re.search(pattern, text_lower, flags=re.UNICODE):
                        log_debug(f"[mention] match_reason=persona_name:{persona_name}")
                        log_debug(
                            f"[mention] ✅ Persona name found: '{persona_name}' - PRIORITY 4b - message is for bot"
                        )
                        return True, None
                    else:
                        log_debug(
                            f"[mention] Persona name '{persona_name}' not found in '{text_lower}'"
                        )
                except Exception:
                    # Fallback to simple substring check if regex fails for some reason
                    if persona_name.lower() in text_lower:
                        log_debug(
                            f"[mention] match_reason=persona_name_fallback:{persona_name}"
                        )
                        log_debug(
                            f"[mention] ✅ Persona name found by fallback: '{persona_name}' - PRIORITY 4b - message is for bot"
                        )
                        return True, None
                    else:
                        log_debug(
                            f"[mention] Persona name '{persona_name}' not found in '{text_lower}' (fallback)"
                        )
            else:
                log_debug("[mention] No persona name available to check")
        except Exception as e:
            log_debug(f"[mention] Error checking persona name: {e}")

    # Priority 4c: Check for persona triggers (aliases/likes/dislikes/interests)
    if message_text:
        try:
            from core.persona_manager import get_persona_manager

            persona_manager = get_persona_manager()
            if persona_manager and persona_manager.check_triggers(message_text):
                log_debug("[mention] match_reason=persona_trigger")
                log_debug(
                    "[mention] ✅ Persona trigger found (aliases/likes/dislikes/interests) - PRIORITY 4c - message is for bot"
                )
                return True, None
        except Exception as e:
            log_debug(f"[mention] Error checking persona triggers: {e}")

    # Priority 5: Check for chat 1:1 using human count (fallback)
    if human_count is not None and human_count == 1:
        log_debug("[mention] match_reason=single_human")
        log_debug(
            "[mention] ✅ Single human in chat - PRIORITY 5 - treating as message for bot"
        )
        return True, None

    # No direct mention found and either multiple humans or unknown count.
    # If we can't determine human_count, treat it as not 1:1 by default.
    if human_count is None:
        log_debug(
            "[mention] No direct mention found and human count unavailable - treating as not 1:1"
        )
        return False, "unknown_human_count"

    log_debug(
        f"[mention] No direct mention found and multiple humans in chat ({human_count})"
    )
    return False, "multiple_humans"
