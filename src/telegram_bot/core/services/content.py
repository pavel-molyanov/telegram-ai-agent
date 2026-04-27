"""Shared content extraction utilities for message handlers.

Extracts text content from Telegram messages (text, photo, video, document,
sticker, voice, video_note). Used by both business and group chat handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.types import Message

from telegram_bot.core.config import MAX_VOICE_SIZE_BYTES
from telegram_bot.core.messages import t
from telegram_bot.core.services.transcriber import TranscriptionError
from telegram_bot.core.utils.fs import sanitize_filename

if TYPE_CHECKING:
    from telegram_bot.core.services.transcriber import Transcriber

logger = logging.getLogger(__name__)


async def extract_content(message: Message, bot: Bot, transcriber: Transcriber) -> str | None:
    """Extract message content as text string.

    Returns:
        str for text/media/voice messages, None for unsupported types.
    """
    # Text message
    if message.text:
        return message.text

    # Photo
    if message.photo:
        caption = message.caption or ""
        if caption:
            return f"{t('cc.photo')} {caption}"
        return t("cc.photo")

    # Video
    if message.video:
        return t("cc.video")

    # Document
    if message.document:
        filename = message.document.file_name
        if filename:
            sanitized = sanitize_filename(filename)
            return t("cc.document_named", name=sanitized)
        return t("cc.document")

    # Sticker
    if message.sticker:
        emoji = message.sticker.emoji or ""
        return t("cc.sticker", emoji=emoji)

    # Voice
    if message.voice:
        return await transcribe_media(
            bot, transcriber, message.voice.file_id, message.voice.file_size, t("cc.voice_label")
        )

    # Video note (circle)
    if message.video_note:
        return await transcribe_media(
            bot,
            transcriber,
            message.video_note.file_id,
            message.video_note.file_size,
            t("cc.videomessage_label"),
        )

    # Unsupported message type
    logger.debug(
        "Unsupported message type in chat %d, message %d — skipping",
        message.chat.id,
        message.message_id,
    )
    return None


async def transcribe_media(
    bot: Bot, transcriber: Transcriber, file_id: str, file_size: int | None, label: str
) -> str:
    """Download voice/video_note, transcribe via Deepgram. Retry once on failure."""
    fail = t("cc.transcription_failed", label=label)

    if file_size and file_size > MAX_VOICE_SIZE_BYTES:
        logger.warning("%s too large: %d bytes", label, file_size)
        return fail

    try:
        file = await bot.get_file(file_id)
        if file.file_path is None:
            return fail
        bio = await bot.download_file(file.file_path)
        if bio is None:
            return fail
        try:
            audio_data: bytes = bio.read()
        finally:
            bio.close()
    except Exception:
        logger.warning("Failed to download %s for transcription", label, exc_info=True)
        return fail

    for attempt in range(2):
        try:
            transcript = await transcriber.transcribe(audio_data)
            if transcript.strip():
                return f"[{label}] {transcript}"
            return fail
        except TranscriptionError:
            if attempt == 0:
                logger.info("%s transcription failed, retrying (attempt 1)", label)
                continue
            logger.warning("%s transcription failed after retry", label, exc_info=True)
            return fail

    return fail
