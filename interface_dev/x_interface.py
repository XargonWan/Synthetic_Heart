"""X (Twitter) interface.

This module provides a minimal interface for posting messages and retrieving
public posts from the X platform using :mod:`snscrape`.  It mirrors the
``TelegramInterface`` API so it can be plugged into the existing action system.
"""

from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, List

# snscrape has compatibility issues with Python 3.12, disable for now
SNSCRAPE_AVAILABLE = False
sntwitter = None

from core.logging_utils import log_info, log_debug, log_warning
from core.core_initializer import register_interface, core_initializer

log_warning("[x_interface] snscrape disabled due to Python 3.12 compatibility issues")


class XInterface:
    """Interface wrapper for the X (Twitter) platform."""

    def __init__(self) -> None:
        self.username = os.environ.get("X_USERNAME")
        if self.username:
            log_debug(f"[x_interface] Using username: {self.username}")
        else:
            log_debug("[x_interface] X_USERNAME not set; timeline features disabled")
        log_info("[x_interface] Registered XInterface")

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this interface."""
        return "x"

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {
            "message_x": {
                "required_fields": ["text"],
                "optional_fields": ["target_user"],
                "description": "Post a message on X. Optionally set 'target_user' to mention someone.",
            },
            "x_timeline_read": {
                "required_fields": [],
                "optional_fields": [],
                "description": "Read the latest posts from the authenticated user's timeline.",
            },
            "x_search": {
                "required_fields": ["query"],
                "optional_fields": [],
                "description": "Search public posts on X using a query string.",
            },
        }

    async def send_message(
        self, payload: Dict[str, Any], original_message: Any | None = None
    ) -> None:
        """Format and log a message post."""

        text = payload.get("text", "")
        target_user = payload.get("target_user")

        if target_user:
            text = f"@{target_user} {text}"

        if not text:
            return

        # Check if this is an autonomous posting (no original_message) that should use auto-response
        if original_message is None and payload.get("autonomous", False):
            # This would be for future autonomous X posting features
            log_debug(
                "[x_interface] Autonomous posting detected, using auto-response system"
            )
            # For now, just log. Future implementation could create a synthetic message
            # and route through request_llm_delivery for LLM-mediated posting decisions

        log_info(f"[x_interface] Message posted: {text}")

    async def _read_timeline(self) -> List[str]:
        """Fetch the latest five posts from ``X_USERNAME`` via snscrape."""

        if not SNSCRAPE_AVAILABLE:
            log_warning("[x_interface] snscrape not available; cannot read timeline")
            return []

        if not self.username:
            log_info("[x_interface] X_USERNAME not configured; cannot read timeline")
            return []

        def fetch() -> List[str]:
            tweets = []
            try:
                for idx, tweet in enumerate(
                    sntwitter.TwitterUserScraper(self.username).get_items()
                ):
                    if idx >= 5:
                        break
                    tweets.append(tweet.content)
            except Exception as e:
                log_warning(f"[x_interface] Error fetching timeline: {e}")
            return tweets

        tweets = await asyncio.to_thread(fetch)
        for t in tweets:
            log_info(f"[x_interface] {t}")
        return tweets

    async def _search(self, query: str) -> List[str]:
        """Search public posts using snscrape."""

        if not SNSCRAPE_AVAILABLE:
            log_warning("[x_interface] snscrape not available; cannot search")
            return []

        if not query:
            return []

        def fetch() -> List[str]:
            results = []
            try:
                for idx, tweet in enumerate(
                    sntwitter.TwitterSearchScraper(query).get_items()
                ):
                    if idx >= 5:
                        break
                    results.append(tweet.content)
            except Exception as e:
                log_warning(f"[x_interface] Error searching: {e}")
            return results

        results = await asyncio.to_thread(fetch)
        for r in results:
            log_info(f"[x_interface] {r}")
        return results

    async def execute_action(
        self, action: dict, context: dict, bot: Any, original_message: dict
    ):
        """Execute actions using this interface."""

        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "message_x":
            await self.send_message(payload, original_message)
        elif action_type == "x_timeline_read":
            return await self._read_timeline()
        elif action_type == "x_search":
            query = payload.get("query", "")
            return await self._search(query)
        else:
            log_info(f"[x_interface] Unsupported action type: {action_type}")

    @staticmethod
    def get_interface_instructions() -> str:
        """Return specific usage instructions for the X interface."""
        return "Use interface: x to post or retrieve data from X. For direct messages or mentions, set 'target_user'."

    @staticmethod
    def validate_payload(action_type: str, payload: dict) -> list:
        """Validate payload for X actions."""
        errors = []

        if action_type == "message_x":
            if not payload.get("text"):
                errors.append("text is required for message_x")
        elif action_type == "x_search":
            if not payload.get("query"):
                errors.append("query is required for x_search")
        # x_timeline_read requires no specific fields

        return errors

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        """Return detailed instructions for each action type."""
        if action_name == "message_x":
            return {
                "description": "Send a message or reply on X.",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello X!",
                        "description": "The message text to send.",
                    },
                    "target_user": {
                        "type": "string",
                        "example": "@example",
                        "description": "The username of the recipient.",
                    },
                    "reply_to_message_id": {
                        "type": "string",
                        "example": "1234567890",
                        "description": "Optional ID of the message to reply to",
                        "optional": True,
                    },
                },
            }
        elif action_name == "x_timeline_read":
            return {
                "description": "Read latest posts from authenticated user's timeline",
                "payload": {"interface": "x"},
            }
        elif action_name == "x_search":
            return {
                "description": "Search public posts on X",
                "payload": {"query": "search terms", "interface": "x"},
            }
        return {}


# Expose class for dynamic loading and register interface
INTERFACE_CLASS = XInterface
x_interface = XInterface()
register_interface("x", x_interface)
core_initializer.register_interface("x")
