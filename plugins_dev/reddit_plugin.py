import os
import asyncio
import praw
from core.logging_utils import log_debug, log_info, log_warning, log_error


class RedditPlugin:
    """Action plugin to post submissions and comments to Reddit."""

    def __init__(self):
        try:
            self.reddit = praw.Reddit(
                client_id=os.getenv("REDDIT_CLIENT_ID"),
                client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
                username=os.getenv("REDDIT_USERNAME"),
                password=os.getenv("REDDIT_PASSWORD"),
                user_agent=os.getenv("REDDIT_USER_AGENT", "synth-bot/0.1"),
            )
            log_debug("[reddit_plugin] Initialized")
        except Exception as e:
            self.reddit = None
            log_error(f"[reddit_plugin] Failed to init Reddit: {repr(e)}")

    @property
    def description(self) -> str:
        return "Post messages to Reddit using the message action"

    def get_supported_action_types(self) -> list[str]:
        return ["message_reddit"]

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this plugin interface."""
        return "reddit"

    def get_supported_actions(self) -> dict:
        return {
            "message_reddit": {
                "required_fields": ["text", "target", "title"],
                "optional_fields": [
                    "thread_id"
                ],  # Solo per Reddit, Telegram usa thread_id
                "description": "Post a submission or comment to Reddit",
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name != "message_reddit":
            return None
        return {
            "description": "Send a post or comment on Reddit",
            "payload": {
                "text": {
                    "type": "string",
                    "example": "Post content here",
                    "description": "The content of the post or comment",
                },
                "target": {
                    "type": "string",
                    "example": "r/example_subreddit",
                    "description": "The subreddit to post to",
                },
                "title": {
                    "type": "string",
                    "example": "Optional post title",
                    "description": "Title for new posts",
                    "optional": True,
                },
                "thread_id": {
                    "type": "string",
                    "example": "abc123",
                    "description": "Optional comment thread ID for replies (Reddit only)",
                    "optional": True,
                },
                "interface": {
                    "type": "string",
                    "example": self.get_interface_id(),
                    "description": "Interface identifier",
                    "auto_filled": True,
                },
            },
        }

    def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute a Reddit message action.

        `thread_id` should be a Reddit post ID or comment ID as a string. Per Telegram usare sempre `thread_id`.
        If provided, `text` will be posted as a reply to that thread. If not,
        a new submission is created in the target subreddit using `title`.
        """
        if not self.reddit:
            log_error("[reddit_plugin] Reddit plugin not configured; skipping action")
            return
        if action.get("interface") != "reddit":
            log_debug(
                "[reddit_plugin] Skipping action for interface %s"
                % action.get("interface")
            )
            return

        payload = action.get("payload", {})
        text = payload.get("text", "")
        target = payload.get("target")
        thread_id = payload.get("thread_id")  # expected comment or submission ID
        title = payload.get("title")
        flair_id = payload.get("flair_id")

        if not text or not target:
            log_error("[reddit_plugin] Invalid payload: missing text or target")
            return

        if not thread_id and not title:
            log_error("[reddit_plugin] Missing title for new post; aborting")
            return

        try:
            if thread_id:
                try:
                    comment = self.reddit.comment(thread_id)
                    reply = comment.reply(text)
                except Exception:
                    submission = self.reddit.submission(thread_id)
                    reply = submission.reply(text)
                url = getattr(reply, "permalink", f"/comments/{reply.id}")
                log_info(f"[reddit_plugin] Reply posted: https://reddit.com{url}")
            else:
                subreddit_name = target.lstrip("r/")
                submission = self.reddit.subreddit(subreddit_name).submit(
                    title=title, selftext=text
                )
                if flair_id:
                    try:
                        submission.flair.select(flair_id)
                    except Exception as e:
                        log_warning(f"[reddit_plugin] Failed to set flair: {e}")
                log_info(
                    f"[reddit_plugin] Submission created: https://reddit.com{submission.permalink}"
                )
        except Exception as e:
            log_error(f"[reddit_plugin] Failed to execute action: {e}", e)

    async def handle_custom_action(self, action_type: str, payload: dict):
        if action_type != "message_reddit":
            log_warning(f"[reddit_plugin] Unsupported action type: {action_type}")
            return
        if not self.reddit:
            log_error("[reddit_plugin] Reddit plugin not configured; skipping action")
            return
        action = {"type": "message_reddit", "interface": "reddit", "payload": payload}
        await asyncio.to_thread(self.execute_action, action, {}, None, None)


__all__ = ["RedditPlugin"]
PLUGIN_CLASS = RedditPlugin
