import os
import asyncio
from datetime import datetime
from types import SimpleNamespace

try:
    from dotenv import load_dotenv
except Exception:

    def load_dotenv(*args, **kwargs):
        return False


load_dotenv()

try:
    import asyncpraw
except Exception:  # pragma: no cover - library missing in env
    asyncpraw = None  # type: ignore

from core.logging_utils import log_debug, log_warning, log_error, log_info
from core.transport_layer import universal_send
from core.core_initializer import register_interface, core_initializer
from core.auto_response import request_llm_delivery


class RedditInterface:
    """Minimal Reddit interface using asyncpraw."""

    def __init__(self):
        if asyncpraw is None:
            raise RuntimeError("asyncpraw not available")

        # Check required configuration
        client_id = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")

        if not client_id or not client_secret:
            log_warning(
                "[reddit_interface] REDDIT_CLIENT_ID or REDDIT_CLIENT_SECRET not configured - Reddit interface disabled"
            )
            self.reddit = None
            return

        token = os.getenv("TOKEN")
        self.reddit = asyncpraw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=os.getenv("REDDIT_USER_AGENT", "synth-agent"),
            username=os.getenv("REDDIT_USERNAME"),
            password=os.getenv("REDDIT_PASSWORD"),
            refresh_token=token,
        )
        self._running = False
        register_interface("reddit", self)
        core_initializer.register_interface("reddit")
        log_info("[reddit_interface] Registered RedditInterface")

    # --- interface metadata -------------------------------------------------
    @staticmethod
    def get_interface_id() -> str:
        return "reddit"

    @staticmethod
    def get_supported_action_types() -> list[str]:
        return ["message"]

    @staticmethod
    def get_action_types() -> list[str]:
        """Return action types supported by this interface."""
        return ["message_reddit"]

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "message_reddit": {
                "description": "Send a message or reply on Reddit.",
                "required_fields": ["text", "target"],
                "optional_fields": ["reply_message_id"],
            }
        }

    def get_prompt_instructions(action_name: str) -> dict:
        if action_name == "message_reddit":
            return {
                "description": "Send a message or reply on Reddit.",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello Reddit!",
                        "description": "The message text to send.",
                    },
                    "target": {
                        "type": "string",
                        "example": "example_user",
                        "description": "The username or subreddit.",
                    },
                    "reply_message_id": {
                        "type": "string",
                        "example": "t1_abcdef",
                        "description": "Optional comment/message id to reply to.",
                        "optional": True,
                    },
                },
            }
        return {}

    # --- public API ---------------------------------------------------------
    async def start(self):
        """Begin listening for inbox events and register the interface."""
        if self._running:
            return
        self._running = True
        asyncio.create_task(self._listen_inbox())
        log_debug("[reddit_interface] Listening started and interface registered")

    async def read_feed(self, limit: int = 10):
        front = self.reddit.front
        posts = []
        async for post in front.hot(limit=limit):
            posts.append(post)
        return posts

    async def search(self, query: str, limit: int = 10):
        sub = await self.reddit.subreddit("all")
        results = []
        async for item in sub.search(query, limit=limit):
            results.append(item)
        return results

    async def read_dms(self, limit: int = 10):
        messages = []
        async for msg in self.reddit.inbox.messages(limit=limit):
            messages.append(msg)
        return messages

    async def send_dm(self, username: str, text: str):
        redditor = await self.reddit.redditor(username)
        await redditor.message("message", text)

    async def reply(self, parent_id: str, text: str):
        try:
            comment = await self.reddit.comment(parent_id)
            await comment.reply(text)
        except Exception:
            try:
                message = await self.reddit.inbox.message(parent_id)
                await message.reply(text)
            except Exception as e:
                log_error(f"[reddit_interface] Reply failed: {e}")

    async def follow_subreddit(self, name: str):
        sub = await self.reddit.subreddit(name)
        await sub.subscribe()

    async def unfollow_subreddit(self, name: str):
        sub = await self.reddit.subreddit(name)
        await sub.unsubscribe()

    async def follow_user(self, name: str):
        user = await self.reddit.redditor(name)
        await user.friend()

    async def unfollow_user(self, name: str):
        user = await self.reddit.redditor(name)
        await user.unfriend()

    async def send_message(self, payload: dict, original_message: object | None = None):
        text = payload.get("text", "")
        target = payload.get("target")
        reply_message_id = payload.get("reply_message_id")
        await universal_send(
            self._reddit_send, target, text=text, reply_message_id=reply_message_id
        )

    async def _reddit_send(
        self, target: str, text: str, reply_message_id: str | None = None
    ):
        if reply_message_id:
            try:
                comment = await self.reddit.comment(reply_message_id)
                await comment.reply(text)
                return
            except Exception:
                try:
                    message = await self.reddit.inbox.message(reply_message_id)
                    await message.reply(text)
                    return
                except Exception as e:
                    log_warning(f"[reddit_interface] Reply attempt failed: {e}")
        await self.send_dm(target, text)

    # --- internal helpers ---------------------------------------------------
    async def _listen_inbox(self):
        if asyncpraw is None:
            return
        try:
            async for item in self.reddit.inbox.stream(skip_existing=True):
                wrapper = self._wrap_item(item)
                if wrapper:
                    # Use auto-response system for autonomous Reddit interactions
                    await request_llm_delivery(
                        message=wrapper,
                        interface=self,
                        context={},
                        reason="reddit_autonomous_response",
                    )
        except Exception as e:
            log_error(f"[reddit_interface] Inbox listener stopped: {e}")
            self._running = False

    def _wrap_item(self, item):
        if asyncpraw is None:
            return None
        author = getattr(item, "author", None)
        author_name = author.name if author else "unknown"
        chat = SimpleNamespace(id=author_name, type="private")
        if isinstance(item, asyncpraw.models.Message):
            return SimpleNamespace(
                chat_id=author_name,
                message_id=item.id,
                text=getattr(item, "body", ""),
                date=datetime.fromtimestamp(item.created_utc),
                from_user=SimpleNamespace(
                    id=author_name, full_name=author_name, username=author_name
                ),
                reply_to_message=None,
                chat=chat,
            )
        if isinstance(item, asyncpraw.models.Comment):
            chat = SimpleNamespace(id=item.subreddit.display_name, type="public")
            return SimpleNamespace(
                chat_id=author_name,
                message_id=item.id,
                text=getattr(item, "body", ""),
                date=datetime.fromtimestamp(item.created_utc),
                from_user=SimpleNamespace(
                    id=author_name, full_name=author_name, username=author_name
                ),
                reply_to_message=None,
                chat=chat,
            )
        return None

    @staticmethod
    def get_interface_instructions():
        return (
            "REDDIT INTERFACE INSTRUCTIONS:\n"
            "- Use usernames as targets when sending direct messages.\n"
            "- Provide 'reply_message_id' when replying to comments or messages to maintain context.\n"
            "- Text is plain Markdown.\n"
        )


async def start_reddit_interface():
    log_info("[reddit_interface] start_reddit_interface() function called")

    # Check if Reddit is configured
    if not os.getenv("REDDIT_CLIENT_ID") or not os.getenv("REDDIT_CLIENT_SECRET"):
        log_warning("[reddit_interface] Reddit not configured - skipping startup")
        return

    try:
        log_info("[reddit_interface] Importing core_initializer...")

        log_info("[reddit_interface] Initializing Reddit interface...")

        reddit_interface = RedditInterface()
        if reddit_interface.reddit is None:
            log_warning("[reddit_interface] Reddit interface not properly configured")
            return

        await reddit_interface.start()

        log_info("[reddit_interface] Reddit interface initialized successfully")
    except Exception as e:
        log_error(
            f"[reddit_interface] Error in Reddit interface initialization: {repr(e)}"
        )
        raise


INTERFACE_CLASS = RedditInterface
__all__ = ["RedditInterface"]
