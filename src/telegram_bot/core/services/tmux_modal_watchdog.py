"""Modal detection and alerting for tmux sessions.

Contains three pieces of modal-related logic extracted from the manager
facade:

1. `ModalWatchdog` — background task that periodically probes every active
   session for an idle-modal overlay and posts a Telegram alert.
2. `send_modal_alert` / `send_modal_idle_alert` — free functions that post
   the two alert variants and update the dedup map.
3. `safe_send_and_enter` — capture → send-keys → verify-visible → Enter
   pipeline shared between `send_direct` and `send_stream` (Wave 3 B1 fix).

All three take the manager explicitly — no circular import, and tests can
still `patch.object(mgr, "_send_modal_idle_alert", ...)` on the wrapper
methods in the facade.

Every alert path emits a single `TUI_ALERT_AUDIT` log line so an operator
can run `journalctl -u telegram-bot | grep TUI_ALERT_AUDIT` and see the
cause of every TUI-panel post in the chat. Source/reason form a small
fixed enum (see `_AUDIT_SOURCE_*` / per-call `reason=...` strings); the
last five pane lines are dumped alongside so a misfire is debuggable
without re-running the failing send.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from telegram_bot.core.tui.modal_alert import render_modal_alert, render_modal_idle_alert
from telegram_bot.core.types import ChannelKey

if TYPE_CHECKING:
    from telegram_bot.core.services.tmux_manager import TmuxManager
    from telegram_bot.core.services.tmux_state import TmuxSessionState

logger = logging.getLogger(__name__)

# Max chars we echo from a Telegram/OSError exception into logs. The
# aiogram TelegramBadRequest str() includes the API error *reason*
# ("can't parse entities", "MESSAGE_TOO_LONG", etc.) which is load-
# bearing for triage; we cap the slice so nothing like an echoed-back
# pane or prompt bleeds into journald.
_EXC_MESSAGE_MAX_CHARS = 200

# Audit log: how many trailing pane lines to include alongside every
# TUI_ALERT_AUDIT entry. Five is enough to capture the modal footer +
# the input bar + a context line; tight enough that journald lines
# stay readable when grep'd.
_AUDIT_PANE_TAIL_LINES = 5

# Audit `source` enum — fixed set so a grep/jq filter is reliable.
AUDIT_SOURCE_SEND_FAILED = "modal_alert_send_failed"
AUDIT_SOURCE_IDLE_WATCHDOG = "modal_alert_idle_watchdog"
AUDIT_SOURCE_USER_COMMAND = "user_command"
AUDIT_SOURCE_TUI_BUTTON = "tui_button"


def _safe_exc_message(exc: BaseException) -> str:
    """Truncated `str(exc)` for logging. Strips line breaks so the record
    stays on one journalctl line, and caps length so long error payloads
    don't leak into logs."""
    msg = str(exc).replace("\n", " ").replace("\r", " ")
    if len(msg) > _EXC_MESSAGE_MAX_CHARS:
        msg = msg[:_EXC_MESSAGE_MAX_CHARS] + "…"
    return msg


def _pane_tail_for_audit(pane: str, max_lines: int = _AUDIT_PANE_TAIL_LINES) -> str:
    """Return the last `max_lines` non-empty-trailing lines of `pane` as a
    single string suitable for embedding in a log record."""
    if not pane:
        return ""
    lines = pane.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[-max_lines:])


def log_alert_audit(
    *,
    source: str,
    reason: str,
    session_name: str,
    message_id: int | str,
    pane: str,
    timing_ms: int = 0,
) -> None:
    """Emit the canonical TUI_ALERT_AUDIT log line.

    Single source of truth for the log shape — every alert/TUI-panel
    poster (send_modal_alert, send_modal_idle_alert, /tui handler,
    ui.btn_tui handler) calls this so `grep TUI_ALERT_AUDIT` returns
    every panel that ever appeared in chat with reason+source+pane_tail.
    """
    pane_tail = _pane_tail_for_audit(pane)
    logger.info(
        "TUI_ALERT_AUDIT: source=%s reason=%r session=%s msg_id=%s timing_ms=%d pane_tail=\n%s",
        source,
        reason,
        session_name,
        message_id,
        timing_ms,
        pane_tail,
    )


class ModalWatchdog:
    """Runs `check_channel` on every active channel every `interval_sec`.

    Delegates per-channel work to a callable supplied at construction —
    keeps the watchdog ignorant of session state and alerting details,
    which live in the facade. Exceptions in `check_channel` are caught
    and logged so one bad channel never kills the loop for the others.
    """

    def __init__(
        self,
        *,
        check_channel: Callable[[ChannelKey], Awaitable[None]],
        channels_snapshot: Callable[[], list[ChannelKey]],
    ) -> None:
        self._check_channel = check_channel
        self._channels_snapshot = channels_snapshot
        self._task: asyncio.Task[None] | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, interval_sec: float) -> None:
        """Launch the background task. Idempotent."""
        if self.is_running():
            return
        self._task = asyncio.create_task(self._loop(interval_sec), name="modal-watchdog")
        logger.info("TUI_IO: modal watchdog started interval=%.1fs", interval_sec)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self, interval_sec: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_sec)
                for channel_key in self._channels_snapshot():
                    try:
                        await self._check_channel(channel_key)
                    except Exception:
                        logger.warning(
                            "TUI_IO: modal watchdog error channel=%s",
                            channel_key,
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            raise


async def send_modal_alert(
    manager: TmuxManager,
    channel_key: ChannelKey,
    state: TmuxSessionState,
    prompt: str,
    pane: str,
    *,
    reason: str = "unspecified",
) -> None:
    """Post the modal-blocked alert to Telegram. No-op if live-buffer
    wiring is absent.

    `reason` names the specific code path that decided to alert (see the
    string enum maintained at the call sites — `_safe_send_codex` and
    `_safe_send_and_enter` in `tmux_manager.py`). It is recorded in the
    `TUI_ALERT_AUDIT` log so every Telegram-side panel can be traced
    back to its trigger via `journalctl | grep`.
    """
    bot = manager._bot
    if bot is None or state.session_id is None:
        return
    text = ""
    try:
        text, keyboard = render_modal_alert(
            prompt=prompt,
            pane=pane,
            session_id=state.session_id,
            chat_id=channel_key[0],
            thread_id=channel_key[1],
        )
        sent = await bot.send_message(  # type: ignore[attr-defined]
            chat_id=channel_key[0],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=channel_key[1],
        )
        message_id = getattr(sent, "message_id", "?")
        logger.info(
            "TUI_IO: modal alert posted session=%s channel=%s message_id=%s",
            state.session_name,
            channel_key,
            message_id,
        )
        log_alert_audit(
            source=AUDIT_SOURCE_SEND_FAILED,
            reason=reason,
            session_name=state.session_name,
            message_id=message_id,
            pane=pane,
        )
        # Dedup key for the watchdog — store the pane we alerted on;
        # the watchdog skips re-alerting until the pane actually changes.
        manager._last_modal_pane[channel_key] = pane
    except (TimeoutError, TelegramAPIError, OSError) as exc:
        logger.warning(
            "TUI_IO: modal alert failed session=%s exc=%s reason=%r text_len=%d",
            state.session_name,
            type(exc).__name__,
            _safe_exc_message(exc),
            len(text),
        )


async def send_modal_idle_alert(
    manager: TmuxManager,
    channel_key: ChannelKey,
    state: TmuxSessionState,
    pane: str,
    *,
    reason: str = "modal_idle_detected",
) -> None:
    """Watchdog counterpart of `send_modal_alert`: same layout minus the
    user-prompt echo. Writes to the same `_last_modal_pane` dedup map so
    a second watchdog tick on the unchanged pane is a no-op."""
    bot = manager._bot
    if bot is None or state.session_id is None:
        return
    text = ""
    try:
        text, keyboard = render_modal_idle_alert(
            pane=pane,
            session_id=state.session_id,
            chat_id=channel_key[0],
            thread_id=channel_key[1],
        )
        sent = await bot.send_message(  # type: ignore[attr-defined]
            chat_id=channel_key[0],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=channel_key[1],
        )
        message_id = getattr(sent, "message_id", "?")
        logger.info(
            "TUI_IO: modal idle-alert posted session=%s channel=%s message_id=%s",
            state.session_name,
            channel_key,
            message_id,
        )
        log_alert_audit(
            source=AUDIT_SOURCE_IDLE_WATCHDOG,
            reason=reason,
            session_name=state.session_name,
            message_id=message_id,
            pane=pane,
        )
        manager._last_modal_pane[channel_key] = pane
    except (TimeoutError, TelegramAPIError, OSError) as exc:
        logger.warning(
            "TUI_IO: modal idle-alert failed session=%s exc=%s reason=%r text_len=%d",
            state.session_name,
            type(exc).__name__,
            _safe_exc_message(exc),
            len(text),
        )
