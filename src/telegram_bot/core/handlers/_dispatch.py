"""Shared enqueue helper for message handlers (text/voice/photo/forward).

Before this module, each of the four input handlers inlined the same
6-8 lines at the end of their callback: resolve target_session_id →
optionally build+inject reply context → enqueue with tmux-aware
suppress_notification flag. The differences across handlers are in the
reply-resolution strategy (e.g. photo/forward use the batcher's text
reply, text/voice use the message's own reply_to), so those stay in
the handlers. The final enqueue step is invariant and lives here.

Keep this module intentionally small: no orchestration, no ensure_ready,
no tmux-active short-circuit — those decisions belong to each handler
because they have handler-specific quirks (text.py runs switch_session
for reply-to-resume in tmux; voice.py dispatches from a batch snapshot;
photo.py short-circuits on all-failed downloads; forward.py skips
inject_reply_context entirely). Keeping that logic in the handlers
avoids a "mega-dispatch" that needs a policy argument per quirk.

Dependency direction is one-way: handlers → _dispatch → streaming →
services. Do NOT import anything from handlers here — that would
create a cycle.
"""

from __future__ import annotations

import logging

from aiogram.types import Message

from telegram_bot.core.handlers.streaming import build_reply_context, inject_reply_context
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


def enqueue_prompt(
    key: ChannelKey,
    prompt: str,
    source_msg: Message,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    *,
    target_session_id: str | None,
    inject_reply_if_no_target: bool,
) -> None:
    """Final enqueue step shared by text/voice/photo/forward handlers.

    - If `target_session_id` is None AND `inject_reply_if_no_target` is True,
      builds a reply context from source_msg.reply_to_message and injects it
      into the prompt. This covers the case where the user replied to a bot
      message that has no associated session (briefing, reminder).
    - forward.py passes `inject_reply_if_no_target=False`: forwarded batches
      already carry their own structure; injecting a reply quote would
      duplicate the user's intent.
    - Always enqueues with `suppress_notification=tmux_manager.is_active(key)`
      because in tmux mode the CC TUI provides its own position feedback
      (thinking placeholder, live buffer) — the queue's "added to position N"
      message is redundant.
    """
    if target_session_id is None and inject_reply_if_no_target:
        reply_context = build_reply_context(source_msg)
        if reply_context:
            prompt = inject_reply_context(prompt, reply_context)
    logger.info(
        "MSG_TRACE enqueue_prompt channel=%s msg=%d prompt_len=%d target_sid=%s",
        key,
        source_msg.message_id,
        len(prompt),
        target_session_id,
    )
    message_queue.enqueue(
        key,
        prompt,
        source_msg.message_id,
        source_msg,
        target_session_id=target_session_id,
        suppress_notification=tmux_manager.is_active(key),
    )
