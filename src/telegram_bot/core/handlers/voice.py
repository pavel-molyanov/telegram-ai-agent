"""Voice message handler — defers transcription to ForwardBatcher for co-batching."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.types import Message

from telegram_bot.core.config import MAX_VOICE_SIZE_BYTES
from telegram_bot.core.handlers._dispatch import enqueue_prompt
from telegram_bot.core.handlers.streaming import (
    ensure_exec_mode_ready,
    resolve_reply_target,
    send_to_tmux_if_active,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import channel_key

if TYPE_CHECKING:
    from telegram_bot.core.handlers.forward import ForwardBatcher

logger = logging.getLogger(__name__)

router = Router(name="voice")


@router.message(F.voice)
async def handle_voice(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    inbox_reply_handler: Callable[[Message, MessageQueue], Awaitable[bool]] | None = None,
) -> None:
    key = channel_key(message)
    logger.debug("Voice message from user %s", message.from_user and message.from_user.id)

    # Check file size before anything else
    if message.voice and message.voice.file_size and message.voice.file_size > MAX_VOICE_SIZE_BYTES:
        await message.answer(t("ui.voice_too_large"))
        return

    # Show recognizing status immediately; ForwardBatcher._process_batch will edit it
    # with the transcript once transcription completes.
    recognizing_msg = await message.answer(t("ui.recognizing_voice"))

    async def on_voice_batch(voice_snapshot: list[tuple[Message, Message]]) -> None:
        """Handle voice-only batch after transcription has completed.

        By the time this runs, forward_batcher._process_batch has already transcribed
        every voice in the snapshot and appended the results to cb.comment. We read
        them back via get_comment(), then dispatch to tmux or the message queue.
        """
        transcripts = forward_batcher.get_comment(key)
        if not transcripts:
            # All transcriptions failed or returned nothing — nothing to send
            return

        # Check inbox reply for every voice in the batch — if any one is a reply
        # to an inbox report, that path wins and the executor is launched.
        if inbox_reply_handler is not None:
            for voice_msg, _ in voice_snapshot:
                if await inbox_reply_handler(voice_msg, message_queue):
                    return

        # Transcripts already carry the "[Voice, transcription]:" short prefix
        # (added by _try_transcribe_voice). Joining them produces the full prompt
        # without double-prefixing.
        prompt = "\n".join(transcripts)

        # Reply target and context track the last voice in the batch
        last_voice_msg = voice_snapshot[-1][0]

        if (
            last_voice_msg.reply_to_message is not None
            and session_manager.reply_requires_provider_switch(
                last_voice_msg.reply_to_message.message_id,
                key,
            )
        ):
            await last_voice_msg.answer(t("ui.tui_session_missing"))
            return

        if not await ensure_exec_mode_ready(
            key, topic_config, tmux_manager, session_manager, last_voice_msg
        ):
            return

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, prompt, last_voice_msg, tmux_manager):
            return

        target_session_id = resolve_reply_target(last_voice_msg, session_manager)
        enqueue_prompt(
            key,
            prompt,
            last_voice_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            inject_reply_if_no_target=True,
        )

    forward_batcher.add_voice(key, message, recognizing_msg, on_voice_batch)
