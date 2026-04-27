"""MessageQueue — per-chat message queuing and batching for Telegram bot."""

from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    """One item in the message queue — may contain multiple batched prompts."""

    entries: list[tuple[int, str]]  # (message_id, prompt)
    source_messages: list[Message]
    target_session_id: str | None = None


@dataclass
class ChatQueue:
    """Per-chat queue state."""

    items: collections.deque[QueueItem] = field(default_factory=collections.deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    error_count: int = 0


# Type alias for the process callback
ProcessCallback = Callable[
    [ChannelKey, str, list[Message], str | None],
    Awaitable[None],
]


def _combine_prompts(entries: list[tuple[int, str]]) -> str:
    """Combine prompt entries into a single prompt string.

    Single entry: return the prompt as-is.
    Multiple entries (sorted by message_id): numbered Russian format.
    """
    sorted_entries = sorted(entries, key=lambda e: e[0])

    if len(sorted_entries) == 1:
        return sorted_entries[0][1]

    count = len(sorted_entries)
    parts = [t("cc.batch_during_processing", count=count)]
    for i, (_, prompt) in enumerate(sorted_entries, 1):
        parts.append(f"\n{i}. {prompt}")

    return "\n".join(parts)


class MessageQueue:
    """Central orchestrator for per-chat message processing."""

    def __init__(
        self,
        bot: Bot,
        session_manager: SessionManager,
        process_callback: ProcessCallback,
    ) -> None:
        self._bot = bot
        self._session_manager = session_manager
        self._process_callback = process_callback
        self._queues: dict[ChannelKey, ChatQueue] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _get_queue(self, channel_key: ChannelKey) -> ChatQueue:
        if channel_key not in self._queues:
            self._queues[channel_key] = ChatQueue()
        return self._queues[channel_key]

    def is_busy(self, channel_key: ChannelKey) -> bool:
        """Return True if the channel has active processing or queued items.

        Does NOT create a queue entry for unknown keys — a no-op check must
        not pollute `_queues` with empty `ChatQueue` instances.
        """
        queue = self._queues.get(channel_key)
        if queue is None:
            return False
        return queue.lock.locked() or bool(queue.items)

    def enqueue(
        self,
        channel_key: ChannelKey,
        prompt: str,
        message_id: int,
        source_message: Message,
        target_session_id: str | None = None,
        suppress_notification: bool = False,
    ) -> None:
        """Add a message to the channel's queue.

        Synchronous — no await between state check and mutation to prevent races.
        suppress_notification=True skips the "added to queue" Telegram message.
        Use this when the caller already provides meaningful feedback (e.g. tmux mode).
        """
        queue = self._get_queue(channel_key)

        if not queue.lock.locked():
            # First message — start processing immediately, no notification
            item = QueueItem(
                entries=[(message_id, prompt)],
                source_messages=[source_message],
                target_session_id=target_session_id,
            )
            queue.items.append(item)
            logger.info(
                "MSG_TRACE queue_enqueue channel=%s msg=%d action=immediate_start "
                "prompt_len=%d target_sid=%s",
                channel_key,
                message_id,
                len(prompt),
                target_session_id,
            )
            self._start_processing(channel_key)
            return

        # Processing is active — try to batch or create new item
        target_key = target_session_id
        batched = False

        # Find last item in deque with matching target
        for item in reversed(queue.items):
            if item.target_session_id == target_key:
                item.entries.append((message_id, prompt))
                item.source_messages.append(source_message)
                batched = True
                # Find position of this item in queue (1-based)
                position = list(queue.items).index(item) + 1
                break

        if batched:
            notification = self._build_notification(
                is_batch=True,
                position=position,
                target_session_id=target_session_id,
            )
        else:
            # Create new QueueItem
            item = QueueItem(
                entries=[(message_id, prompt)],
                source_messages=[source_message],
                target_session_id=target_session_id,
            )
            queue.items.append(item)
            position = len(queue.items)
            notification = self._build_notification(
                is_batch=False,
                position=position,
                target_session_id=target_session_id,
            )
        logger.info(
            "MSG_TRACE queue_enqueue channel=%s msg=%d action=%s position=%d "
            "prompt_len=%d target_sid=%s",
            channel_key,
            message_id,
            "appended_to_existing" if batched else "new_item",
            position,
            len(prompt),
            target_session_id,
        )

        if suppress_notification:
            return

        # Send notification (fire and forget, prevent GC via _background_tasks)
        task = asyncio.create_task(self._send_notification(channel_key, notification))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _build_notification(
        self,
        *,
        is_batch: bool,
        position: int,
        target_session_id: str | None,
    ) -> str:
        """Build composable notification text."""
        if is_batch:
            text = t("ui.queue_added_batch", position=position)
        else:
            text = t("ui.queue_added", position=position)

        if target_session_id is not None:
            short_id = target_session_id[:6]
            text += t("ui.queue_session_suffix", sid=short_id)

        return text

    async def _send_notification(self, channel_key: ChannelKey, text: str) -> None:
        """Send a notification message to the channel."""
        chat_id, thread_id = channel_key
        try:
            await self._bot.send_message(
                chat_id,
                text,
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning(
                "TelegramBadRequest sending queue notification to %s",
                channel_key,
                exc_info=True,
            )
        except Exception:
            logger.exception("Failed to send queue notification to %s", channel_key)

    def _start_processing(self, channel_key: ChannelKey) -> None:
        """Start the processing loop for a channel."""
        task = asyncio.create_task(self._process_next(channel_key))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_next(self, channel_key: ChannelKey) -> None:
        """Process all items in the queue, one at a time, under lock."""
        queue = self._get_queue(channel_key)

        async with queue.lock:
            while queue.items:
                item = queue.items.popleft()

                # Sort entries by message_id (ascending) and combine prompts
                combined_prompt = _combine_prompts(item.entries)
                logger.info(
                    "MSG_TRACE queue_dequeue channel=%s msg_ids=%s prompt_len=%d "
                    "target_sid=%s remaining=%d",
                    channel_key,
                    [mid for mid, _ in item.entries],
                    len(combined_prompt),
                    item.target_session_id,
                    len(queue.items),
                )

                try:
                    await self._process_callback(
                        channel_key,
                        combined_prompt,
                        item.source_messages,
                        item.target_session_id,
                    )
                    queue.error_count = 0
                except Exception:
                    # Drop semantics: the item was already popped above and is
                    # not re-enqueued. The backoff throttles the NEXT item so
                    # consecutive failures don't storm downstream; it is not a
                    # per-item retry. error_count resets on first success.
                    queue.error_count += 1
                    backoff_sec = min(2**queue.error_count, 30)
                    logger.warning(
                        "Queue item dropped for %s after callback error "
                        "(consecutive failures=%d, next-item backoff=%ds)",
                        channel_key,
                        queue.error_count,
                        backoff_sec,
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff_sec)

    async def clear(self, channel_key: ChannelKey) -> None:
        """Clear the queue for a channel: wait for active processing, then discard pending items."""
        queue = self._get_queue(channel_key)
        pending_count = len(queue.items)
        is_active = queue.lock.locked()
        logger.info(
            "Clearing queue for %s: %d pending items, active=%s",
            channel_key,
            pending_count,
            is_active,
        )

        # Kill CC subprocess first so processing finishes quickly
        await self._session_manager.cancel(channel_key)

        # Wait for processing to finish, then clear under lock
        async with queue.lock:
            queue.items.clear()

    async def cancel(self, channel_key: ChannelKey) -> bool:
        """Cancel current processing: kill CC process and clear queue. Preserve session.

        Returns True if there was an active process or queued items to cancel.
        """
        queue = self._get_queue(channel_key)
        dropped = len(queue.items)
        queue.items.clear()

        stopped = await self._session_manager.cancel(channel_key)
        return stopped or dropped > 0

    async def shutdown(self) -> None:
        """Cancel background tasks, clear all queues."""
        # Cancel notification tasks
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Clear all channel queues (items only — lock releases naturally)
        for _channel_key, queue in self._queues.items():
            queue.items.clear()

        self._queues.clear()
