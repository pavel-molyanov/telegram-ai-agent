"""Shared types for the telegram bot."""

from __future__ import annotations

from aiogram.types import Message

ChannelKey = tuple[int, int | None]
"""Compound key (chat_id, thread_id) identifying a unique conversation channel.

When thread_id is None, represents classic (non-topic) chat mode.
"""


def channel_key(message: Message) -> ChannelKey:
    """Extract ChannelKey from an aiogram Message."""
    return (message.chat.id, message.message_thread_id)
