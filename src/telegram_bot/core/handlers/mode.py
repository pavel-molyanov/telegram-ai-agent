"""New-chat handler — "Новый чат" reply button resets the current topic session."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from telegram_bot.core.handlers.commands import _reset_channel
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)

router = Router(name="mode")


@router.message(F.text == t("ui.btn_new_chat"))
async def handle_new_chat_button(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.info("New chat (topic reset) for %s", key)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )
