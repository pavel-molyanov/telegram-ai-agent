"""Photo and document handler — validates files, batches via ForwardBatcher."""

from __future__ import annotations

import asyncio
import logging
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, F, Router
from aiogram.types import Message

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
from telegram_bot.core.types import ChannelKey, channel_key
from telegram_bot.core.utils.fs import sanitize_filename

if TYPE_CHECKING:
    from telegram_bot.core.handlers.forward import ForwardBatcher

logger = logging.getLogger(__name__)

router = Router(name="photo")

_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
_MAX_FILE_SIZE_MB = 20


def is_file_too_large(file_size: int | None) -> bool:
    """Check if file exceeds the size limit. None size is treated as acceptable."""
    if file_size is None:
        return False
    return file_size > _MAX_FILE_SIZE


def _get_tmp_dir(file_cache_dir: str) -> Path:
    """Return the media-download cache directory as a Path."""
    return Path(file_cache_dir)


def ensure_tmp_dir(file_cache_dir: str) -> None:
    """Create the file cache directory if it doesn't exist."""
    tmp_dir = _get_tmp_dir(file_cache_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Ensured file cache dir exists: %s", tmp_dir)


_TMP_FILE_MAX_AGE_SECONDS = 24 * 3600  # 24 hours


def cleanup_old_tmp_files(file_cache_dir: str) -> int:
    """Delete files older than 24 hours from the file cache directory.

    Returns the number of files deleted.
    """
    tmp_dir = _get_tmp_dir(file_cache_dir)

    if not tmp_dir.exists():
        logger.info("Tmp dir does not exist, skipping cleanup: %s", tmp_dir)
        return 0

    threshold = time.time() - _TMP_FILE_MAX_AGE_SECONDS
    deleted_count = 0

    for item in tmp_dir.iterdir():
        try:
            # Use lstat() to avoid following symlinks (TOCTOU mitigation)
            st = item.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime < threshold:
                item.unlink(missing_ok=True)
                deleted_count += 1
                logger.debug("Deleted old tmp file: %s", item.name)
        except OSError as e:
            logger.warning("Failed to process tmp file %s: %s", item.name, e)

    if deleted_count > 0:
        logger.info("Deleted %d old files from %s", deleted_count, tmp_dir)
    else:
        logger.info("No old files to delete in %s", tmp_dir)

    return deleted_count


# --- Media download and formatting ---


async def _download_and_format_media(
    message: Message, bot: Bot, tmp_dir: Path
) -> dict[str, Any] | None:
    """Download a single photo or document and return metadata dict.

    Returns None if the message has no photo or document.
    Returns dict with 'path': None on download failure.
    """
    if message.photo:
        photo = message.photo[-1]
        timestamp = int(time.time())
        filename = f"{timestamp}_{photo.file_unique_id}.jpg"
        dest_path = tmp_dir / filename
        try:
            await bot.download(photo.file_id, destination=dest_path)
            return {
                "type": "photo",
                "caption": message.caption or "",
                "path": str(dest_path),
            }
        except Exception:
            logger.warning("Failed to download photo in media batch", exc_info=True)
            return {
                "type": "photo",
                "caption": message.caption or "",
                "path": None,
                "error": "failed to download photo",
            }

    if message.document:
        doc = message.document
        timestamp = int(time.time())
        original_name = sanitize_filename(doc.file_name or "file")
        dest_filename = f"{timestamp}_{doc.file_unique_id}_{original_name}"
        dest_path = tmp_dir / dest_filename
        caption = message.caption or ""
        try:
            await bot.download(doc.file_id, destination=dest_path)
            return {
                "type": "document",
                "name": original_name,
                "mime": doc.mime_type or "unknown",
                "caption": caption,
                "path": str(dest_path),
            }
        except Exception:
            logger.warning("Failed to download document in media batch", exc_info=True)
            return {
                "type": "document",
                "name": original_name,
                "mime": doc.mime_type or "unknown",
                "path": None,
                "error": f"failed to download document: {original_name}",
            }

    return None


def _format_single_document(item: dict[str, Any]) -> str:
    """Format a single document item (backwards-compatible with old format)."""
    name = item.get("name", "file")
    mime = item.get("mime", "unknown")
    caption = item.get("caption", "")
    path = item.get("path")
    if path is None:
        return t(
            "cc.document_error",
            name=name,
            mime=mime,
            error=item.get("error", t("cc.error_generic_label")),
        )
    file_ref = t("cc.attached_file", path=path)
    header = t("cc.document_full", name=name, mime=mime)
    if caption:
        return f"{header}\n{t('cc.caption', caption=caption)}\n{file_ref}"
    return f"{header}\n{file_ref}"


def _format_media_item(item: dict[str, Any]) -> str:
    """Format a single media item — used both for single-item and batch renders.

    Photo-with-empty-caption must NOT emit the "Photo with caption" header;
    otherwise the prompt reads as if the user wrote an empty caption (wave 2.4
    regression). Bare [Photo] + File: path is the correct empty-caption form.
    """
    if item["type"] == "photo":
        caption = item.get("caption", "")
        path = item.get("path")
        if path is None:
            return t("cc.photo_error", error=item.get("error", t("cc.error_generic_label")))
        if caption:
            return t("cc.file_caption", caption=caption, path=path)
        return f"{t('cc.photo')}\n{t('cc.file_path', path=path)}"
    elif item["type"] == "document":
        return _format_single_document(item)
    return t("cc.unknown_file_type")


def _format_media_prompt(items: list[dict[str, Any]], comment: list[str] | None = None) -> str:
    """Format media items into a prompt for CC.

    Single item: backwards-compatible format (no numbering).
    Multiple items: numbered list with header.
    """
    if len(items) == 1 and not comment:
        return _format_media_item(items[0])

    lines = [t("cc.files_batch", count=len(items))]
    if comment:
        combined = "\n".join(comment)
        lines.append(t("cc.user_comment", comment=combined))
    lines.append("")

    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {_format_media_item(item)}")
        lines.append("")

    return "\n".join(lines)


# --- Handlers ---


def _make_media_callback(
    key: ChannelKey,
    fallback_msg: Message,
    bot: Bot,
    session_manager: SessionManager,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> Callable[[list[Message]], Awaitable[None]]:
    """Create the on_media_batch callback shared by photo and document handlers."""

    async def on_media_batch(raw_messages: list[Message]) -> None:
        comment = forward_batcher.get_comment(key)
        text_reply = forward_batcher.get_text_reply_to_message(key)
        last_msg = forward_batcher.get_last_message(key) or fallback_msg

        reply_for_provider_check = text_reply if text_reply else last_msg.reply_to_message
        if reply_for_provider_check is not None and session_manager.reply_requires_provider_switch(
            reply_for_provider_check.message_id,
            key,
        ):
            await last_msg.answer(t("ui.tui_session_missing"))
            return

        if not await ensure_exec_mode_ready(
            key, topic_config, tmux_manager, session_manager, last_msg
        ):
            return

        if len(raw_messages) > 1:
            await last_msg.answer(t("ui.processing_files"))

        tmp_dir = _get_tmp_dir(session_manager.file_cache_dir)
        results = await asyncio.gather(
            *[_download_and_format_media(m, bot, tmp_dir) for m in raw_messages]
        )
        items = [r for r in results if r is not None]

        if not items:
            await last_msg.answer(t("ui.download_error"))
            return

        # If ALL downloads failed, show error instead of sending empty prompt
        if all(item.get("path") is None for item in items):
            await last_msg.answer(t("ui.download_error"))
            return

        prompt = _format_media_prompt(items, comment if comment else None)

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, prompt, last_msg, tmux_manager):
            return

        if text_reply:
            # Pass `key` so the cross-channel guard fires — otherwise a reply
            # targeting a message stored under a different topic silently
            # imports that topic's session_id.
            target_session_id = session_manager.resolve_reply_session(text_reply.message_id, key)
        else:
            target_session_id = resolve_reply_target(last_msg, session_manager)

        enqueue_prompt(
            key,
            prompt,
            last_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            inject_reply_if_no_target=True,
        )

    return on_media_batch


@router.message(F.photo)
async def handle_photo(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Handle photo messages: validate size, add to media batcher."""
    key = channel_key(message)
    logger.debug("Photo message from user %s", message.from_user and message.from_user.id)

    if not message.photo:
        return

    photo = message.photo[-1]
    if is_file_too_large(photo.file_size):
        await message.answer(t("ui.file_too_large", size=_MAX_FILE_SIZE_MB))
        return

    callback = _make_media_callback(
        key,
        message,
        bot,
        session_manager,
        forward_batcher,
        message_queue,
        tmux_manager,
        topic_config,
    )
    forward_batcher.add_media(key, message, callback)


@router.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Handle document messages: validate size, add to media batcher."""
    key = channel_key(message)
    logger.debug("Document message from user %s", message.from_user and message.from_user.id)

    if not message.document:
        return

    doc = message.document
    if is_file_too_large(doc.file_size):
        await message.answer(t("ui.file_too_large", size=_MAX_FILE_SIZE_MB))
        return

    callback = _make_media_callback(
        key,
        message,
        bot,
        session_manager,
        forward_batcher,
        message_queue,
        tmux_manager,
        topic_config,
    )
    forward_batcher.add_media(key, message, callback)
