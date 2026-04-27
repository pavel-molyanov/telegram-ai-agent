"""Small helpers around aiogram calls.

Centralizes the try/except TelegramRetryAfter + sleep + retry pattern that
used to be copy-pasted at every send/edit site. Every flood wait is logged
once with its retry_after and a label so journalctl tells us where it hit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)

# Opt-in verbose send logging. INFO-level previews of every delivered message
# quickly drown the journal in user-content snippets; off by default, flip via
# `TELEGRAM_VERBOSE_SEND_LOGS=1` in .env for incident triage.
_VERBOSE_SEND_LOGS = os.environ.get("TELEGRAM_VERBOSE_SEND_LOGS") == "1"


@dataclass(frozen=True)
class SendOutcome:
    """Result of a send attempt through send_html_with_fallback.

    message_id is non-None only on a successful delivery (HTML, retried
    HTML, or plain-text fallback). fatal=True signals the bot was blocked
    by the user (TelegramForbiddenError) — callers that iterate over
    multiple sends should stop further attempts.
    """

    message_id: int | None
    fatal: bool = False


async def send_html_with_fallback(
    *,
    send_html: Callable[[], Awaitable[Any]],
    send_plain: Callable[[], Awaitable[Any]],
    label: str,
    flood_retry_limit: float = 120.0,
) -> SendOutcome:
    """Send an HTML message with three-level fallback.

    Order of attempts:
      1. send_html() — HTML parse_mode attempt.
      2. On TelegramRetryAfter: sleep (up to flood_retry_limit),
         retry send_html() once. A second failure is swallowed and does
         NOT cascade to plain-text fallback — flood_wait indicates API
         load, not HTML validity, so trying plain immediately would
         almost certainly flood-wait again.
      3. On TelegramBadRequest (first HTML attempt only): retry via
         send_plain() without parse_mode. This is the usual rescue path
         for malformed HTML produced upstream by markdown_to_html —
         tags become visible as text but the message is delivered.
      4. On TelegramForbiddenError: log and return SendOutcome(None,
         fatal=True). Callers use fatal to short-circuit further sends
         in the same flow (e.g. send_failed latch in streaming.on_event).

    The helper never propagates exceptions from send_html/send_plain —
    every failure is logged under the `label` and reflected in the
    returned SendOutcome. Callers must treat message_id=None as
    "didn't ship, we logged it, move on".

    Callers pass send_html and send_plain as pre-bound callables so the
    helper is transport-agnostic — the same signature works for
    Message.answer, Bot.send_message, and any other aiogram send/edit
    variant.
    """
    path = "html"  # which branch produced the final sent (for the success log)
    try:
        sent = await send_html()
    except TelegramRetryAfter as e:
        logger.warning(
            "Telegram flood wait on %s: retry_after=%ds",
            label,
            e.retry_after,
        )
        if e.retry_after > flood_retry_limit:
            logger.warning(
                "Giving up on %s — retry_after %ds exceeds %.0fs limit",
                label,
                e.retry_after,
                flood_retry_limit,
            )
            return SendOutcome(message_id=None)
        await asyncio.sleep(e.retry_after)
        try:
            sent = await send_html()
            path = "html_retry"
        except Exception:
            logger.warning("Retry of %s failed", label, exc_info=True)
            return SendOutcome(message_id=None)
    except TelegramBadRequest as e:
        # We deliberately don't filter "message is not modified" — this
        # helper wraps NEW sends, not edits, so that error should not occur.
        logger.warning("%s HTML send failed: %s — retrying as plain text", label, e)
        try:
            sent = await send_plain()
            path = "plain_fallback"
        except Exception:
            logger.warning("Plain-text fallback for %s failed", label, exc_info=True)
            return SendOutcome(message_id=None)
    except TelegramForbiddenError as e:
        logger.warning("Bot blocked on %s: %s", label, e)
        return SendOutcome(message_id=None, fatal=True)
    except Exception:
        logger.warning("%s failed", label, exc_info=True)
        return SendOutcome(message_id=None)

    message_id = getattr(sent, "message_id", None)
    # Success log: makes it visible in journalctl that a send completed,
    # which branch delivered it, the resulting message_id, payload length
    # and an 80-char preview. Preview + length make the log
    # self-sufficient for post-mortems of "bot went silent" / "client
    # didn't show the message" reports — without them the journal only
    # proved a send happened, not what Telegram accepted.
    # Preview is a user-content snippet — DEBUG by default, INFO only when
    # TELEGRAM_VERBOSE_SEND_LOGS=1 opts in (e.g. during incident triage).
    raw_text = getattr(sent, "text", None)
    text_str = raw_text if isinstance(raw_text, str) else ""
    preview = text_str[:80].replace("\n", " ")
    level = logging.INFO if _VERBOSE_SEND_LOGS else logging.DEBUG
    logger.log(
        level,
        "%s delivered via %s, message_id=%s, len=%d, preview=%r",
        label,
        path,
        message_id,
        len(text_str),
        preview,
    )
    return SendOutcome(message_id=message_id)
