"""Authentication middleware — whitelist by user ID."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


def _is_forum_topic_event(event: TelegramObject) -> bool:
    """Service messages emitted by Telegram when a forum topic is created/edited.

    These bypass the user whitelist because `from_user` is whoever triggered the
    change — often the bot itself (when CC creates topics via Bot API), or a
    group admin with topic permissions. The forum_topic handler needs to see them
    regardless of who fired them, otherwise topic_config.json is never updated
    when the bot creates a topic.
    """
    if not isinstance(event, Message):
        return False
    return event.forum_topic_created is not None or event.forum_topic_edited is not None


class AuthMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: list[int]) -> None:
        self.allowed_user_ids = set(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if _is_forum_topic_event(event):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None or user.id not in self.allowed_user_ids:
            # Unauthorized access is a security signal — leave a WARNING so
            # `journalctl -p warning -u telegram-bot` surfaces a probe or
            # a leaked-token pattern. DEBUG hid this in the stream of
            # per-event traces (wave 2.8 review finding).
            logger.warning("Ignored message from unauthorized user: %s", getattr(user, "id", None))
            return None
        return await handler(event, data)
