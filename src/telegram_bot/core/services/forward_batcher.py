"""Forward message batching: debounced buffers, media download, prompt formatter.

Extracted from `core/handlers/forward.py`. The handler module keeps only
the router + `handle_forward` entry point; everything else — debounce
batcher, media download, sender extraction, prompt formatting — lives
here so the handler stays small and the batching pieces can be unit-
tested in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    Message,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from aiogram.utils.text_decorations import HtmlDecoration

from telegram_bot.core.config import MAX_VOICE_SIZE_BYTES
from telegram_bot.core.handlers.photo import (
    _get_tmp_dir,
    is_file_too_large,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.transcriber import Transcriber, TranscriptionError
from telegram_bot.core.types import ChannelKey
from telegram_bot.core.utils.fs import sanitize_filename

logger = logging.getLogger(__name__)

_html_decorator = HtmlDecoration()


def unparse_entities(text: str | None, entities: list[MessageEntity] | None) -> str:
    """Convert text + entities into HTML string preserving links and formatting."""
    if not text:
        return ""
    if not entities:
        return text
    return _html_decorator.unparse(text, entities)


_DEFAULT_DEBOUNCE_SEC = 0.5
_DEFAULT_TEXT_DEBOUNCE_SEC = 1.5
_MAX_BATCH_SIZE = 50

# --- Data models ---


@dataclass
class SenderInfo:
    name: str
    username: str | None = None
    user_id: int | None = None
    post_url: str | None = None


@dataclass
class ForwardedMessage:
    sender: SenderInfo
    date: datetime
    text: str
    file_paths: list[str] = field(default_factory=list)


# --- Sender extraction ---


def _extract_sender_info(origin: object | None) -> SenderInfo:
    """Extract sender name, username, and user_id from forward_origin."""
    if isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        parts = [user.first_name]
        if user.last_name:
            parts.append(user.last_name)
        return SenderInfo(
            name=" ".join(parts),
            username=user.username,
            user_id=user.id,
        )
    if isinstance(origin, MessageOriginHiddenUser):
        name = origin.sender_user_name or t("cc.unknown_sender")
        return SenderInfo(name=name)
    if isinstance(origin, MessageOriginChannel):
        username = origin.chat.username
        if username:
            post_url = f"https://t.me/{username}/{origin.message_id}"
        else:
            # Private channel: strip -100 prefix from chat_id
            abs_id = abs(origin.chat.id)
            link_id = str(abs_id)[3:] if str(abs_id).startswith("100") else str(abs_id)
            post_url = f"https://t.me/c/{link_id}/{origin.message_id}"
        return SenderInfo(
            name=origin.chat.title or t("cc.unknown_channel"),
            username=username,
            post_url=post_url,
        )
    if isinstance(origin, MessageOriginChat):
        return SenderInfo(
            name=origin.sender_chat.title or t("cc.unknown_chat"),
            username=origin.sender_chat.username,
        )
    return SenderInfo(name=t("cc.unknown_sender"))


# --- Media processing ---


async def _edit_recog_msg(recog_msg: Message, ui_text: str) -> None:
    """Replace the "⌛ Распознаю…" status with transcript or error text.

    Why a retry: a co-chat `TelegramRetryAfter` on the shared aiogram bot pool
    can bubble up here (incident 2026-04-23 19:46 — flood wait in one topic
    blocked the edit in another). Previously `contextlib.suppress(Exception)`
    hid every failure, so the user stayed stuck on the "⌛ Распознаю…" bubble
    forever even though the transcript had already flown to CC. One retry
    after the server-suggested delay covers that case; any other failure is
    surfaced as a warning instead of silently dropped.
    """
    try:
        await recog_msg.edit_text(ui_text)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(max(float(exc.retry_after), 0.1))
        try:
            await recog_msg.edit_text(ui_text)
        except Exception as retry_exc:
            logger.warning(
                "recog_msg edit failed after retry (chat %s, msg %s): %s",
                getattr(getattr(recog_msg, "chat", None), "id", None),
                getattr(recog_msg, "message_id", None),
                retry_exc,
            )
    except Exception as exc:
        logger.warning(
            "recog_msg edit failed (chat %s, msg %s): %s",
            getattr(getattr(recog_msg, "chat", None), "id", None),
            getattr(recog_msg, "message_id", None),
            exc,
        )


async def _try_transcribe_voice(
    message: Message,
    bot: Bot,
    transcriber: Transcriber,
) -> tuple[bool, str]:
    """Download and transcribe a voice message.

    Returns (success, text). On success `text` is the raw transcript; callers
    format it for UI (🎤 prefix) or CC prompt ([Voice, transcription]: prefix)
    as needed. On failure `text` is the localized error message ready to show.
    """
    try:
        if message.voice is None:
            return False, t("cc.voice_failed_full")
        if message.voice.file_size and message.voice.file_size > MAX_VOICE_SIZE_BYTES:
            return False, t("cc.voice_too_large")
        file = await bot.get_file(message.voice.file_id)
        if file.file_path is None:
            return False, t("cc.voice_failed_full")
        bio = await bot.download_file(file.file_path)
        if bio is None:
            return False, t("cc.voice_failed_full")
        try:
            audio_data: bytes = bio.read()
        finally:
            bio.close()
        transcript = await transcriber.transcribe(audio_data)
        if not transcript.strip():
            return False, t("cc.voice_empty")
        return True, transcript
    except TranscriptionError:
        logger.warning(
            "Failed to transcribe forwarded voice (message_id=%s)",
            message.message_id,
            exc_info=True,
        )
        return False, t("cc.voice_failed_full")
    except Exception:
        logger.warning(
            "Failed to download forwarded voice (message_id=%s)",
            message.message_id,
            exc_info=True,
        )
        return False, t("cc.voice_failed_full")


async def _transcribe_voice(
    message: Message,
    bot: Bot,
    transcriber: Transcriber,
) -> str:
    """Transcribe a forwarded voice message, returning CC-prompt-ready text."""
    success, text = await _try_transcribe_voice(message, bot, transcriber)
    if success:
        return f"{t('cc.voice_transcript_short')} {text}"
    return text


async def _download_photo(
    message: Message,
    bot: Bot,
    tmp_dir: Path,
) -> Path | None:
    """Download highest-res photo to tmp dir."""
    try:
        if not message.photo:
            return None
        photo = message.photo[-1]
        if is_file_too_large(photo.file_size):
            logger.warning("Forwarded photo too large: %s", photo.file_size)
            return None
        timestamp = int(time.time())
        filename = f"{timestamp}_{photo.file_unique_id}.jpg"
        dest_path = tmp_dir / filename
        await bot.download(photo.file_id, destination=dest_path)
        return dest_path
    except Exception:
        logger.warning(
            "Failed to download forwarded photo (message_id=%s, file_id=%s)",
            message.message_id,
            message.photo[-1].file_id if message.photo else "?",
            exc_info=True,
        )
        return None


async def _download_document(
    message: Message,
    bot: Bot,
    tmp_dir: Path,
) -> Path | None:
    """Download document with sanitized name to tmp dir."""
    try:
        if not message.document:
            return None
        doc = message.document
        if is_file_too_large(doc.file_size):
            logger.warning("Forwarded document too large: %s", doc.file_size)
            return None
        timestamp = int(time.time())
        original_name = sanitize_filename(doc.file_name or "file")
        dest_filename = f"{timestamp}_{doc.file_unique_id}_{original_name}"
        dest_path = tmp_dir / dest_filename
        await bot.download(doc.file_id, destination=dest_path)
        return dest_path
    except Exception:
        logger.warning(
            "Failed to download forwarded document (message_id=%s, filename=%s)",
            message.message_id,
            doc.file_name,
            exc_info=True,
        )
        return None


async def _process_forwarded_message(
    message: Message,
    bot: Bot,
    transcriber: Transcriber,
    file_cache_dir: str,
) -> ForwardedMessage:
    """Process a single forwarded message: extract info, download media, transcribe voice."""
    sender = _extract_sender_info(message.forward_origin)
    origin = message.forward_origin
    date = origin.date if origin else message.date

    # 10-field dump of per-forward content flags: DEBUG — useful for
    # "what shape did this forward have?" triage, too noisy for INFO.
    logger.debug(
        "Processing forwarded msg id=%s: text=%s, document=%s, photo=%s, "
        "voice=%s, video=%s, audio=%s, sticker=%s, caption=%s, content_type=%s",
        message.message_id,
        bool(message.text),
        bool(message.document),
        bool(message.photo),
        bool(message.voice),
        bool(message.video),
        bool(message.audio),
        bool(message.sticker),
        bool(message.caption),
        message.content_type,
    )

    # Text-only message — simple case
    if message.text:
        text = unparse_entities(message.text, message.entities)
        return ForwardedMessage(sender=sender, date=date, text=text)

    parts: list[str] = []
    file_paths: list[str] = []
    tmp_dir = _get_tmp_dir(file_cache_dir)

    # Voice — transcribe
    if message.voice:
        transcript = await _transcribe_voice(message, bot, transcriber)
        parts.append(transcript)

    # Photo — download
    if message.photo:
        photo_path = await _download_photo(message, bot, tmp_dir)
        if photo_path:
            file_paths.append(str(photo_path))
            parts.append(t("cc.photo"))
        else:
            parts.append(t("cc.photo_failed"))

    # Document — download
    if message.document:
        doc_path = await _download_document(message, bot, tmp_dir)
        doc_name = message.document.file_name or t("cc.file_default")
        if doc_path:
            file_paths.append(str(doc_path))
            parts.append(t("cc.document_named", name=doc_name))
        else:
            parts.append(t("cc.document_failed", name=doc_name))

    # Other media — text labels only
    if message.video:
        parts.append(t("cc.video"))
    if message.sticker:
        parts.append(t("cc.sticker", emoji=message.sticker.emoji or ""))
    if message.video_note:
        parts.append(t("cc.videomessage"))
    if message.audio:
        parts.append(t("cc.audio", title=message.audio.title or t("cc.audio_untitled")))

    # Caption
    if message.caption:
        parts.append(unparse_entities(message.caption, message.caption_entities))

    text = " ".join(parts) if parts else t("cc.empty_message")
    return ForwardedMessage(
        sender=sender,
        date=date,
        text=text,
        file_paths=file_paths,
    )


# --- Batch formatting ---

_FORWARDED_TAG_RE = re.compile(r"</?forwarded-data\b[^>]*>", re.IGNORECASE)


def sanitize_forwarded_content(text: str) -> str:
    """Escape angle brackets in forwarded content to prevent prompt injection.

    Replaces '<' with '&lt;' only where it forms a forwarded-data tag pattern
    (opening or closing, case-insensitive). This prevents injected content from
    breaking out of the forwarded-data delimiter boundary while keeping most
    text unchanged.
    """
    return _FORWARDED_TAG_RE.sub(lambda m: m.group().replace("<", "&lt;"), text)


def _format_sender(sender: SenderInfo) -> str:
    """Format sender info for the prompt."""
    if sender.username:
        return f"{sender.name} (@{sender.username})"
    return sender.name


def _format_batch_prompt(messages: list[ForwardedMessage], comment: list[str] | None = None) -> str:
    """Format a batch of forwarded messages into a prompt for CC.

    Uses a randomized delimiter tag for defense-in-depth against prompt injection.
    """
    delimiter = f"forwarded-data-{secrets.token_hex(4)}"
    lines = [t("cc.forward_batch", count=len(messages))]
    if comment:
        combined_comment = "\n".join(comment)
        lines.append(t("cc.user_comment", comment=combined_comment))
    lines.append("")
    for i, msg in enumerate(messages, 1):
        # msg.text may contain HTML from unparse_entities() (e.g. <a href="...">, <b>).
        # sanitize_forwarded_content() only escapes <forwarded-data> tags, preserving HTML.
        sanitized_text = sanitize_forwarded_content(msg.text)
        lines.append(t("cc.forward_message_header", index=i))
        lines.append(t("cc.forward_from", name=_format_sender(msg.sender)))
        if msg.sender.post_url:
            lines.append(t("cc.forward_post_link", link=msg.sender.post_url))
        lines.append(t("cc.forward_date", date=msg.date.strftime("%Y-%m-%d %H:%M")))
        lines.append(f"<{delimiter}>{sanitized_text}</{delimiter}>")
        for fp in msg.file_paths:
            lines.append(t("cc.attached_file", path=fp))
        lines.append("")
    return "\n".join(lines)


# --- Batcher ---


@dataclass
class ChatBatch:
    """Per-chat batch state: buffer, timer, callbacks, and metadata."""

    buffer: list[Message] = field(default_factory=list)
    media_buffer: list[Message] = field(default_factory=list)
    # Pairs of (voice_message, recognizing_status_message). Paired so _process_batch
    # can edit the status message with the transcript result.
    voice_buffer: list[tuple[Message, Message]] = field(default_factory=list)
    # Snapshot of voice_buffer taken at the start of _process_batch, kept on the
    # batch so voice callbacks can read it via the batch reference.
    voice_snapshot: list[tuple[Message, Message]] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None
    processing_task: asyncio.Task[None] | None = None
    last_message: Message | None = None
    comment: list[str] = field(default_factory=list)
    # Telegram message_id of every text added via `add_text`, in arrival
    # order. Logged in MSG_TRACE lines so a `journalctl | grep msg=42`
    # walks the message from forward_batcher → callback → tmux pty.
    message_ids: list[int] = field(default_factory=list)
    text_reply_to_message: Message | None = None
    forward_callback: Callable[[list[Message]], Awaitable[None]] | None = None
    text_callback: Callable[[str, Message], Awaitable[None]] | None = None
    media_callback: Callable[[list[Message]], Awaitable[None]] | None = None
    voice_callback: Callable[[list[tuple[Message, Message]]], Awaitable[None]] | None = None


class ForwardBatcher:
    """Collects raw forwarded messages and comments per chat with debounce timer."""

    def __init__(
        self,
        bot: Bot | None = None,
        transcriber: Transcriber | None = None,
        debounce_sec: float = _DEFAULT_DEBOUNCE_SEC,
        text_debounce_sec: float = _DEFAULT_TEXT_DEBOUNCE_SEC,
        max_batch_size: int = _MAX_BATCH_SIZE,
    ) -> None:
        self._bot = bot
        self._transcriber = transcriber
        self._debounce_sec = debounce_sec
        self._text_debounce_sec = text_debounce_sec
        self._max_batch_size = max_batch_size
        self._batches: dict[ChannelKey, ChatBatch] = {}

    def add(
        self,
        channel_key: ChannelKey,
        msg: Message,
        on_batch: Callable[[list[Message]], Awaitable[None]],
    ) -> None:
        """Add a raw forwarded message to the buffer and reset debounce timer."""
        batch = self._batches.setdefault(channel_key, ChatBatch())
        batch.buffer.append(msg)
        batch.last_message = msg
        batch.forward_callback = on_batch

        self._reset_timer(channel_key)

    def add_media(
        self,
        channel_key: ChannelKey,
        msg: Message,
        on_media_batch: Callable[[list[Message]], Awaitable[None]],
    ) -> None:
        """Add a direct photo/document to the media buffer and reset debounce timer."""
        batch = self._batches.setdefault(channel_key, ChatBatch())
        batch.media_buffer.append(msg)
        batch.last_message = msg
        batch.media_callback = on_media_batch

        self._reset_timer(channel_key)

    def add_voice(
        self,
        channel_key: ChannelKey,
        msg: Message,
        recognizing_msg: Message,
        on_voice_batch: Callable[[list[tuple[Message, Message]]], Awaitable[None]],
    ) -> None:
        """Add a voice message to the voice buffer and reset debounce timer.

        `recognizing_msg` is the "recognizing voice…" status message shown to the user
        by handle_voice. _process_batch edits it with the transcript once ready.
        """
        batch = self._batches.setdefault(channel_key, ChatBatch())
        batch.voice_buffer.append((msg, recognizing_msg))
        batch.last_message = msg
        batch.voice_callback = on_voice_batch

        self._reset_timer(channel_key)

    def add_text(
        self,
        channel_key: ChannelKey,
        text: str,
        source_msg: Message,
        on_text: Callable[[str, Message], Awaitable[None]],
    ) -> None:
        """Add a text message to batch. If forwards or media arrive, text becomes comment."""
        batch = self._batches.setdefault(channel_key, ChatBatch())
        batch.comment.append(text)
        batch.message_ids.append(source_msg.message_id)
        batch.last_message = source_msg
        batch.text_reply_to_message = source_msg.reply_to_message
        batch.text_callback = on_text

        msg_id = source_msg.message_id
        msg_ids = list(batch.message_ids)
        if batch.buffer:
            logger.info(
                "MSG_TRACE add_text channel=%s msg=%d batched_with=%s (existing forward batch)",
                channel_key,
                msg_id,
                msg_ids,
            )
        elif batch.media_buffer:
            logger.info(
                "MSG_TRACE add_text channel=%s msg=%d batched_with=%s (existing media batch)",
                channel_key,
                msg_id,
                msg_ids,
            )
        else:
            logger.info(
                "MSG_TRACE add_text channel=%s msg=%d batched_with=%s",
                channel_key,
                msg_id,
                msg_ids,
            )

        self._reset_timer(channel_key)

    def _reset_timer(self, channel_key: ChannelKey) -> None:
        """Cancel existing debounce timer and start new one. Never touches processing_task."""
        batch = self._batches.get(channel_key)
        if batch and batch.timer:
            batch.timer.cancel()

        try:
            # Force-flush if batch is full — start as non-cancellable processing task
            buf_len = (
                len(batch.buffer) + len(batch.media_buffer) + len(batch.voice_buffer)
                if batch
                else 0
            )
            if buf_len >= self._max_batch_size:
                collected = self._collect(channel_key)
                if channel_key in self._batches:
                    self._batches[channel_key].timer = None
                    self._batches[channel_key].processing_task = asyncio.create_task(
                        self._run_processing(channel_key, collected),
                    )
                return

            # Text waiting for forwards/media/voice — use longer debounce so Telegram
            # has time to deliver other items sent together with the comment
            if (
                batch
                and batch.comment
                and not batch.buffer
                and not batch.media_buffer
                and not batch.voice_buffer
            ):
                wait = self._text_debounce_sec
            else:
                wait = self._debounce_sec

            # Start new debounce timer
            timer = asyncio.create_task(
                self._debounce(channel_key, wait),
            )
            if channel_key in self._batches:
                self._batches[channel_key].timer = timer
        except RuntimeError:
            # Event loop closing — clean up to prevent memory leak
            self._cleanup(channel_key)
            logger.warning("Failed to create timer task for %s", channel_key)

    async def _notify_error(self, channel_key: ChannelKey) -> None:
        """Send error notification to user. Suppresses all exceptions to prevent infinite loops."""
        if self._bot is None:
            return
        chat_id, thread_id = channel_key
        try:
            await self._bot.send_message(
                chat_id,
                t("ui.forward_error"),
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning(
                "TelegramBadRequest sending error notification to %s",
                channel_key,
                exc_info=True,
            )
        except Exception:
            logger.error("Failed to send error notification to %s", channel_key, exc_info=True)

    async def _debounce(
        self,
        channel_key: ChannelKey,
        wait_sec: float,
    ) -> None:
        """Wait for debounce, then start non-cancellable processing.

        Serializes per-channel: if a previous `_run_processing` task is
        still in flight (slow text_callback waiting on `start_session`,
        for example), this debounce waits for it to complete before
        starting its own. While waiting, more messages may arrive and
        be appended to the same batch — they will all roll into the
        next `_process_batch` invocation as one combined send.

        Without this serialization, parallel `_run_processing` tasks
        on the same channel competed for `_lazy_start_locks` /
        `_channel_locks` and at least one user message was silently
        dropped (regression observed 2026-04-27 01:17 UTC: "какая
        погода?" lost while claude was cold-starting).
        """
        try:
            logger.info("Debounce started for %s, waiting %.1fs", channel_key, wait_sec)
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            # Timer was reset by new message — don't clean up, new timer will handle it
            logger.info("Debounce cancelled for %s", channel_key)
            return

        # Wait for any in-flight processing on this channel to finish
        # before we collect+process. If we don't, the previous batch's
        # text_cb might still be running its long start_session while we
        # spin up a parallel _process_batch — last-write-wins on
        # `batch.text_callback` and shared `cb.comment` dropped messages
        # in prod.
        batch = self._batches.get(channel_key)
        if batch is not None and batch.processing_task is not None:
            in_flight = batch.processing_task
            if not in_flight.done():
                logger.info(
                    "Debounce for %s waiting on in-flight processing_task",
                    channel_key,
                )
                # Narrow `Exception` (mirrors `_flush_all` below) — let
                # CancelledError / KeyboardInterrupt / SystemExit
                # propagate so shutdown actually shuts down.
                with contextlib.suppress(Exception):
                    await in_flight

        # Phase 2: collect and start non-cancellable processing
        collected = self._collect(channel_key)
        batch = self._batches.get(channel_key)
        if batch is None:
            return

        logger.info("Debounce complete for %s, batch size: %d", channel_key, len(collected))
        batch.timer = None  # Debounce is done
        batch.processing_task = asyncio.create_task(
            self._run_processing(channel_key, collected),
        )

    async def _run_processing(self, channel_key: ChannelKey, collected: list[Message]) -> None:
        """Phase 2 (non-cancellable): Process batch with error handling and conditional cleanup."""
        try:
            await self._process_batch(channel_key, collected)
        except asyncio.CancelledError:
            logger.warning("Processing cancelled, batch data may be lost for %s", channel_key)
        except Exception:
            logger.exception("Batch callback failed for %s", channel_key)
            await self._notify_error(channel_key)
        finally:
            batch = self._batches.get(channel_key)
            if batch is not None:
                batch.processing_task = None
                # Only cleanup if no new messages are pending
                has_pending = (
                    batch.buffer or batch.media_buffer or batch.comment or batch.voice_buffer
                )
                if not has_pending and batch.timer is None:
                    self._cleanup(channel_key)

    async def _process_batch(self, channel_key: ChannelKey, batch: list[Message]) -> None:
        """Process collected batch with appropriate callback."""
        cb = self._batches.get(channel_key)
        if cb is None:
            return

        # Snapshot and clear voice buffer BEFORE any await. Race condition protection:
        # reassigning (not .clear()) means any voice arriving during transcription
        # goes into a fresh list, not the snapshot we're about to process.
        voice_snapshot = list(cb.voice_buffer)
        cb.voice_buffer = []
        cb.voice_snapshot = voice_snapshot

        # Transcribe voice messages in parallel. For each result:
        #   - UI: edit recog_msg with "🎤 {text}" on success, raw error on failure
        #   - CC prompt: append "[Voice, transcription]: {text}" to cb.comment (success only)
        # Failed transcripts never reach CC as content.
        if voice_snapshot and self._bot and self._transcriber:
            results = await asyncio.gather(
                *[
                    _try_transcribe_voice(voice_msg, self._bot, self._transcriber)
                    for voice_msg, _ in voice_snapshot
                ]
            )
            for (_, recog_msg), (success, text) in zip(voice_snapshot, results, strict=True):
                ui_text = f"\U0001f3a4 {text}" if success else text
                await _edit_recog_msg(recog_msg, ui_text)
                if success:
                    cb.comment.append(f"{t('cc.voice_transcript_short')} {text}")

        comment = list(cb.comment)
        comment_snapshot_size = len(cb.comment)
        msg_ids_snapshot = list(cb.message_ids)
        msg_ids_snapshot_size = len(cb.message_ids)
        forward_cb = cb.forward_callback
        media_cb = cb.media_callback
        text_cb = cb.text_callback
        voice_cb = cb.voice_callback
        source_msg = cb.last_message
        media = list(cb.media_buffer)

        logger.info(
            "MSG_TRACE process_batch channel=%s msg_ids=%s forwards=%d media=%d voice=%d "
            "comment=%s forward_cb=%s media_cb=%s text_cb=%s voice_cb=%s",
            channel_key,
            msg_ids_snapshot,
            len(batch),
            len(media),
            len(voice_snapshot),
            bool(comment),
            bool(forward_cb),
            bool(media_cb),
            bool(text_cb),
            bool(voice_cb),
        )

        if batch and forward_cb:
            # Have forwards — use forward callback (comment incl. voice transcripts fetched inside)
            logger.info("Calling forward callback for %s", channel_key)
            await forward_cb(batch)
            # Partial clear (snapshot prefix only). A blanket
            # `cb.comment.clear()` here would also drop captions/text
            # added via `add_text` during the await above, AND leave
            # `cb.message_ids` partially populated → data loss plus
            # index drift between the two parallel lists.
            del cb.comment[:comment_snapshot_size]
            del cb.message_ids[:msg_ids_snapshot_size]
            # The trailing `del cb.comment[:comment_snapshot_size]`
            # below is now a no-op for this path — already drained.
            comment_snapshot_size = 0
            msg_ids_snapshot_size = 0
        if media and media_cb:
            # Have direct media — use media callback (comment fetched inside)
            logger.info("Calling media callback for %s with %d items", channel_key, len(media))
            await media_cb(media)
        elif not batch and not media and voice_snapshot and voice_cb:
            # Voice-only batch — voice callback reads transcripts from get_comment()
            logger.info(
                "Calling voice callback for %s with %d items", channel_key, len(voice_snapshot)
            )
            await voice_cb(voice_snapshot)
        elif not batch and not media and not voice_snapshot and comment and text_cb and source_msg:
            # Only text, no forwards/media/voice — join multiple texts and process
            combined_text = "\n".join(comment)
            logger.info(
                "MSG_TRACE text_callback_dispatch channel=%s msg_ids=%s combined_len=%d",
                channel_key,
                msg_ids_snapshot,
                len(combined_text),
            )
            await text_cb(combined_text, source_msg)
        elif not batch and not media and not voice_snapshot:
            logger.warning(
                "MSG_TRACE no_callback_matched channel=%s msg_ids=%s — message lost!",
                channel_key,
                msg_ids_snapshot,
            )

        # Clear ONLY the items that were in the snapshot. New texts that
        # arrived via `add_text` during the (potentially long) `text_cb`
        # await stay in `cb.comment` for the NEXT debounce/batch — that's
        # how serialized processing preserves messages sent during a
        # cold-start. A blanket `cb.comment.clear()` here would drop them.
        del cb.comment[:comment_snapshot_size]
        del cb.message_ids[:msg_ids_snapshot_size]
        cb.media_buffer.clear()
        cb.voice_snapshot = []

    def _cleanup(self, channel_key: ChannelKey) -> None:
        """Clean up all state for a channel."""
        self._batches.pop(channel_key, None)

    def clear(self, channel_key: ChannelKey) -> None:
        """Clear all batcher state for a channel (mode switch, /new)."""
        batch = self._batches.get(channel_key)
        if batch and batch.timer:
            batch.timer.cancel()
        self._cleanup(channel_key)

    def _collect(self, channel_key: ChannelKey) -> list[Message]:
        """Collect and clear the batch buffer for a channel."""
        batch = self._batches.get(channel_key)
        if batch is None:
            return []
        msgs = batch.buffer
        batch.buffer = []
        return msgs

    def get_last_message(self, channel_key: ChannelKey) -> Message | None:
        """Get the last source message for a channel (without removing)."""
        batch = self._batches.get(channel_key)
        return batch.last_message if batch else None

    def get_comment(self, channel_key: ChannelKey) -> list[str]:
        """Get the comment for a channel batch (non-destructive, returns copy)."""
        batch = self._batches.get(channel_key)
        return list(batch.comment) if batch else []

    def get_text_reply_to_message(self, channel_key: ChannelKey) -> Message | None:
        """Get the text comment's reply_to_message (non-destructive)."""
        batch = self._batches.get(channel_key)
        return batch.text_reply_to_message if batch else None

    async def _flush_all(self) -> None:
        """Flush all pending batches in parallel. Errors are logged, not raised."""
        keys = list(self._batches.keys())
        if not keys:
            return

        async def _flush_channel(channel_key: ChannelKey) -> None:
            try:
                # Await any in-flight processing task first
                batch = self._batches.get(channel_key)
                if batch and batch.processing_task:
                    with contextlib.suppress(Exception):
                        await batch.processing_task

                # Re-read batch (might have been cleaned up by processing)
                batch = self._batches.get(channel_key)
                if batch is None:
                    return

                collected = self._collect(channel_key)
                # Process if forwards collected OR media/voice pending OR text-only batch exists
                if collected or batch.media_buffer or batch.comment or batch.voice_buffer:
                    await self._process_batch(channel_key, collected)
            except Exception:
                logger.error(
                    "Failed to flush batch for %s during shutdown",
                    channel_key,
                    exc_info=True,
                )

        await asyncio.gather(*[_flush_channel(k) for k in keys])

    async def shutdown(self) -> None:
        """Flush pending batches, then cancel timers and clear state."""
        # Cancel debounce timers only (not processing tasks — they must complete)
        for batch in self._batches.values():
            if batch.timer:
                batch.timer.cancel()

        # Flush all pending batches with timeout (includes awaiting processing tasks)
        try:
            await asyncio.wait_for(self._flush_all(), timeout=5.0)
        except TimeoutError:
            logger.error("Shutdown flush timed out after 5s, dropping remaining batches")
        except Exception:
            logger.error("Shutdown flush failed unexpectedly", exc_info=True)

        # Clean up all state
        self._batches.clear()
