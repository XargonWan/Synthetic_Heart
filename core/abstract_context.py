# core/abstract_context.py

"""
Abstract context to avoid specific dependencies on interfaces.
"""

from typing import Dict, Any, Optional, Union
from dataclasses import dataclass
from core.interfaces_registry import get_interface_registry


@dataclass
class AbstractUser:
    """Abstract representation of a user."""

    id: Union[int, str]
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    interface_name: Optional[str] = None


@dataclass
class AbstractMessage:
    """Abstract representation of a message."""

    id: Union[int, str]
    text: Optional[str] = None
    user: Optional[AbstractUser] = None
    chat_id: Union[int, str] = None
    interface_path: Optional[str] = None
    interface_name: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class AbstractChat:
    """Abstract representation of a chat."""

    id: Union[int, str]
    name: Optional[str] = None
    type: Optional[str] = None  # 'private', 'group', 'channel', etc.
    interface_name: Optional[str] = None


class AbstractContext:
    """Abstract context to handle operations without specific dependencies."""

    def __init__(
        self,
        interface_name: str,
        user: Optional[AbstractUser] = None,
        message: Optional[AbstractMessage] = None,
        chat: Optional[AbstractChat] = None,
    ):
        self.interface_name = interface_name
        self.user = user
        self.message = message
        self.chat = chat

    def is_trainer(self) -> bool:
        """Check if the current user is the trainer for this interface."""
        if not self.user:
            return False
        registry = get_interface_registry()
        return registry.is_trainer(self.interface_name, self.user.id)

    def get_user_id(self) -> Optional[Union[int, str]]:
        """Get the current user's ID."""
        return self.user.id if self.user else None

    def get_chat_id(self) -> Optional[Union[int, str]]:
        """Get the current chat's ID."""
        if self.chat:
            return self.chat.id
        elif self.message:
            return self.message.chat_id
        return None

    def get_message_text(self) -> Optional[str]:
        """Get the current message's text."""
        return self.message.text if self.message else None
