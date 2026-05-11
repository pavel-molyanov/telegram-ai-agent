"""`/tui` command + `ttui:` callback — manual control of a live CC TUI.

`/tui` captures the current tmux pane and attaches an inline keyboard for
navigation keys, digit replies to permission dialogs, and refresh/expand/close
controls. `/tail` is kept as an alias for one deployment cycle after the
2026-04-23 rename. All I/O goes through `tmux` via
`asyncio.to_thread(subprocess.run, [...])` using list-args (no shell) so
command-injection is impossible.

Stale-keyboard guards (R7):
- topic binding: callback_data embeds `(chat_id, thread_id)`; mismatch with
  `callback.message.chat.id` / `.message_thread_id` → stale alert, no send-keys.
- session epoch: callback_data carries `session_id[:8]`; mismatch with the
  current live session → stale alert (keyboard belongs to a dead/switched
  session, we must not drive the replacement).

Every return path calls `callback.answer()` exactly once — unanswered
callbacks leave Telegram rendering an infinite spinner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_modal_watchdog import (
    AUDIT_SOURCE_TUI_BUTTON,
    AUDIT_SOURCE_USER_COMMAND,
    log_alert_audit,
)
from telegram_bot.core.tui.capture import escape_pane_for_html
from telegram_bot.core.tui.modal_alert import render_modal_idle_alert
from telegram_bot.core.tui.tail_keyboard import (
    KIND_MODAL,
    KIND_PANEL,
    build_tail_keyboard,
    parse_tail_callback,
)
from telegram_bot.core.types import ChannelKey, channel_key

logger = logging.getLogger(__name__)

router = Router(name="tail")

# Telegram `<pre>` content cap — Telegram rejects messages >4096 chars total.
# 3900 leaves headroom for the `<pre>...</pre>` wrapper and the truncation
# prefix. Measured on escaped text, since HTML entities inflate length.
_PANE_MAX_CHARS = 3900
_TRUNCATION_PREFIX = "... (truncated)\n"
# Pause after send-keys before re-capture. Too short -> capture returns pre-
# keypress state and the edit_text below turns into a no-op (Telegram
# rejects it as "message is not modified", swallowed in `_rerender`),
# leaving the user looking at a stale TUI and forcing them to press
# refresh manually. Too long -> user feels lag. Empirical on CC 2.1.118
# (pane 200x50): fast redraws (BSpace, typing a char, opening a menu
# via "?") complete in 15-20 ms; slower state transitions (Escape
# closing a menu, modal dismissal after digit-select) reach 80 ms; and
# when CC is mid-thinking, the redraw may land on the next render tick
# (~200 ms). 500 ms is the UX-2 plan's upper bound and the value the
# user explicitly preferred after the 300 ms first try -- picks
# "definitely enough" over "minimum snappy". Still below the ~600 ms
# where chat UI feels sluggish. 50 ms -- the original value -- missed
# every slow case and surfaced as a user complaint 2026-04-23.
_SEND_KEYS_SETTLE_SEC = 0.5
_CAPTURE_SCROLLBACK_LINES = "200"

# Map from callback `action` to tmux send-keys argument(s). Empty list means
# the branch does not issue send-keys (refresh / close).
_SEND_KEYS_MAP: dict[str, list[str]] = {
    "up": ["Up"],
    "dn": ["Down"],
    "lt": ["Left"],
    "rt": ["Right"],
    "ent": ["Enter"],
    "bsp": ["BSpace"],
    "esc": ["Escape"],
    "esc2": ["Escape", "Escape"],
    "tab": ["Tab"],
    "btab": ["BTab"],
    "cC": ["C-c"],
    "cU": ["C-u"],
    "cO": ["C-o"],
    "cR": ["C-r"],
    "cT": ["C-t"],
    "num0": ["0"],
    "num1": ["1"],
    "num2": ["2"],
    "num3": ["3"],
}

_RECOVERY_TAIL_ACTIONS = {"ent", "num0", "num1", "num2", "num3"}


def _capture_pane_cmd(session_name: str) -> list[str]:
    return [
        "tmux",
        "capture-pane",
        "-t",
        f"={session_name}:",
        "-p",
        "-S",
        f"-{_CAPTURE_SCROLLBACK_LINES}",
    ]


def _send_keys_cmd(session_name: str, keys: list[str]) -> list[str]:
    return ["tmux", "send-keys", "-t", f"={session_name}:", *keys]


def _format_pane_html(raw_pane: str) -> str:
    """HTML-escape a pane snapshot and cap it at `_PANE_MAX_CHARS`.

    Truncates by keeping the tail (last lines of the scrollback are the
    freshest TUI state) and prefixes with a `(truncated)` marker so the user
    knows content was dropped.
    """
    escaped = escape_pane_for_html(raw_pane)
    if len(escaped) > _PANE_MAX_CHARS:
        escaped = _TRUNCATION_PREFIX + escaped[-_PANE_MAX_CHARS:]
    return f"<pre>{escaped}</pre>"


def _resolve_session_name(tmux_manager: TmuxManager, key: ChannelKey) -> str | None:
    return tmux_manager.get_session_name(key)


@router.message(F.text == t("ui.btn_tui"))
async def handle_tui_button(message: Message, tmux_manager: TmuxManager) -> None:
    """Reply-button shortcut for /tui. Aliases the same entry point so the
    user can reach the TUI snapshot with one keyboard tap instead of
    typing `/tui`."""
    await _handle_tail_entry(
        message,
        tmux_manager,
        audit_source=AUDIT_SOURCE_TUI_BUTTON,
        audit_reason="user_pressed_tui_button",
    )


@router.message(Command("tui", "tail"))
async def handle_tail_command(message: Message, tmux_manager: TmuxManager) -> None:
    """Render a TUI snapshot with an inline navigation keyboard.

    Only fires in topics with a live tmux session. For subprocess topics or
    dead tmux the user gets `ui.tail_unavailable` — no capture attempted.
    """
    await _handle_tail_entry(
        message,
        tmux_manager,
        audit_source=AUDIT_SOURCE_USER_COMMAND,
        audit_reason="user_typed_/tui",
    )


async def _handle_tail_entry(
    message: Message,
    tmux_manager: TmuxManager,
    *,
    audit_source: str,
    audit_reason: str,
) -> None:
    """Shared body for `/tui` (typed command) and the reply-button alias.

    Both surface the same TUI snapshot panel; the only difference is the
    `source` recorded in TUI_ALERT_AUDIT — the operator can see whether
    the panel came from a typed command or a reply-keyboard tap.
    """
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tail_unavailable"))
        return

    session_name = _resolve_session_name(tmux_manager, key)
    # `get_expected_epoch` returns the real session_id[:8] when known,
    # otherwise a synthetic 8-hex derived from session_name. The latter
    # path is hit on a startup-modal-blocked codex cold-start (tmux is
    # alive, the user can see the pane and dismiss the modal through
    # the keyboard, but codex hasn't yet written its session_meta).
    epoch = tmux_manager.get_expected_epoch(key)
    if session_name is None or epoch is None:
        # is_active said True, but state vanished between calls — treat as
        # unavailable rather than crashing on None state.
        await message.answer(t("ui.tail_unavailable"))
        return

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            _capture_pane_cmd(session_name),
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.info("TUI_IO: /tui capture-pane failed session=%s", session_name)
        await message.answer(t("ui.tail_unavailable"))
        return

    raw_pane = result.stdout or ""
    pane_html = _format_pane_html(raw_pane)
    keyboard = build_tail_keyboard(
        session_id=epoch,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
    )

    logger.info("TUI_IO: /tui session=%s", session_name)
    sent = await message.answer(pane_html, parse_mode="HTML", reply_markup=keyboard)
    log_alert_audit(
        source=audit_source,
        reason=audit_reason,
        session_name=session_name,
        message_id=getattr(sent, "message_id", "?"),
        pane=raw_pane,
    )


@router.callback_query(F.data.startswith("ttui:"))
async def handle_tail_callback(callback: CallbackQuery, tmux_manager: TmuxManager) -> None:
    """Dispatch a `ttui:*` inline-keyboard press.

    Order of checks is load-bearing: parse → message present → topic binding →
    session alive → epoch match. Every branch ends with exactly one
    `callback.answer()` — unanswered callbacks leave a spinning indicator.
    """
    parsed = parse_tail_callback(callback.data or "")
    if parsed is None:
        # Don't log full callback.data — a crafted payload could write arbitrary
        # strings into journalctl. Prefix + length is enough to debug parse bugs.
        raw = callback.data or ""
        logger.info("TUI_IO: /tui callback invalid prefix=%r len=%d", raw[:10], len(raw))
        await callback.answer()
        return

    message = callback.message
    if not isinstance(message, Message):
        # Covers both None and InaccessibleMessage (old message outside 48h
        # edit window) — neither case lets us edit_reply_markup or re-render.
        await callback.answer()
        return

    # Per-topic isolation (R7): callback must originate from the same chat/thread
    # as the keyboard was bound to. InaccessibleMessage exposes no
    # message_thread_id — fall back to None.
    msg_chat_id = getattr(message.chat, "id", None)
    msg_thread_id = getattr(message, "message_thread_id", None)
    if parsed.chat_id != msg_chat_id or parsed.thread_id != msg_thread_id:
        logger.info(
            "TUI_IO: /tui callback topic mismatch cb=(%s,%s) msg=(%s,%s)",
            parsed.chat_id,
            parsed.thread_id,
            msg_chat_id,
            msg_thread_id,
        )
        await callback.answer(t("ui.tail_keyboard_stale"), show_alert=True)
        return

    key: ChannelKey = (parsed.chat_id, parsed.thread_id)

    if not tmux_manager.is_active(key):
        await callback.answer(t("ui.tail_keyboard_stale"), show_alert=True)
        return

    # Match the keyboard epoch against either the real session_id[:8]
    # or, when codex hasn't yet materialised its session_id (startup-
    # modal cold-start), the synthetic sha1(session_name)[:8]. After
    # the user dismisses the modal and codex writes its session_meta,
    # the real session_id replaces the synthetic and OLD keyboards
    # become stale here — that's the same UX as after `/new`, which is
    # acceptable: the user simply calls /tui to get a fresh keyboard.
    expected_epoch = tmux_manager.get_expected_epoch(key)
    if expected_epoch is None or expected_epoch != parsed.epoch:
        await callback.answer(t("ui.tail_keyboard_stale"), show_alert=True)
        return

    session_name = _resolve_session_name(tmux_manager, key)
    if session_name is None:
        # Should not happen given is_active guard, but defensive.
        await callback.answer(t("ui.tail_keyboard_stale"), show_alert=True)
        return

    action = parsed.action

    # close: delete the whole /tui post (snapshot + keyboard), no send-keys.
    # The prior behaviour was to drop only the reply markup and leave the
    # <pre> pane snapshot in the chat — which piles up noise over a working
    # day. The modal watchdog's dedup (`_last_modal_pane`) remembers the
    # pane we already alerted on, so deleting the post does NOT un-silence
    # the watchdog on a still-open modal: until the pane actually changes,
    # no new alert fires. `edit_reply_markup(None)` could be used instead
    # as a soft undo, but the user prefers a clean chat.
    if action == "close":
        with contextlib.suppress(Exception):
            await message.delete()
        await callback.answer()
        return

    # refresh: re-capture + re-render, no send-keys.
    if action == "refresh":
        await _rerender(
            message=message,
            tmux_manager=tmux_manager,
            session_name=session_name,
            epoch=expected_epoch,
            chat_id=parsed.chat_id,
            thread_id=parsed.thread_id,
            kind=parsed.kind,
        )
        logger.info("TUI_IO: /tui callback action=refresh session=%s", session_name)
        await callback.answer()
        return

    # Navigation / digit actions.
    keys = _SEND_KEYS_MAP.get(action)
    if keys is None:
        # Unknown action for a valid-looking payload (should never hit; parse
        # guards the action). Defensive answer.
        await callback.answer()
        return

    try:
        await asyncio.to_thread(
            subprocess.run,
            _send_keys_cmd(session_name, keys),
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.info("TUI_IO: /tui send-keys failed action=%s session=%s", action, session_name)
        await callback.answer(t("ui.tail_keyboard_stale"), show_alert=True)
        return

    await asyncio.sleep(_SEND_KEYS_SETTLE_SEC)

    await _rerender(
        message=message,
        tmux_manager=tmux_manager,
        session_name=session_name,
        epoch=expected_epoch,
        chat_id=parsed.chat_id,
        thread_id=parsed.thread_id,
        kind=parsed.kind,
    )
    if action in _RECOVERY_TAIL_ACTIONS:
        await tmux_manager.ensure_recovery_tail(key)
    logger.info("TUI_IO: /tui callback action=%s session=%s", action, session_name)
    await callback.answer()


async def _rerender(
    *,
    message: Message,
    tmux_manager: TmuxManager,
    session_name: str,
    epoch: str,
    chat_id: int,
    thread_id: int | None,
    kind: str = KIND_PANEL,
) -> None:
    """Capture the pane and edit the /tui message in place.

    `epoch` is the 8-hex callback-data binding key. It's either the
    real `session_id[:8]` or — when codex hasn't yet materialised its
    session_id (startup-modal-blocked cold-start) — a synthetic
    sha1(session_name)[:8]. `build_tail_keyboard` and the modal-alert
    renderers accept any 8-hex string here; they truncate with `[:8]`
    so passing a pre-computed epoch is a no-op slice.

    `kind` picks the layout:
      - `panel` (default): bare `<pre>pane</pre>` — regular /tui snapshot.
      - `modal`: `<header>\\n\\n<pre>pane</pre>` via `render_modal_idle_alert`
        so the modal-alert header survives button presses. Without this
        branch the first press on a modal-alert message would collapse
        the text to a bare `<pre>` and drop the warning header — users
        then can't tell they're still looking at a modal alert.

    Swallows `TelegramBadRequest("message is not modified")` which Telegram
    returns on idempotent re-renders (e.g. pressing 🔄 twice with no TUI
    change in between).
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            _capture_pane_cmd(session_name),
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # tmux died mid-callback; leave the message as-is, the next tail loop
        # exit will notify the user.
        return

    raw_pane = result.stdout or ""

    if kind == KIND_MODAL:
        text, keyboard = render_modal_idle_alert(
            pane=raw_pane,
            session_id=epoch,
            chat_id=chat_id,
            thread_id=thread_id,
        )
    else:
        text = _format_pane_html(raw_pane)
        keyboard = build_tail_keyboard(
            session_id=epoch,
            chat_id=chat_id,
            thread_id=thread_id,
        )

    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest:
        # "message is not modified" or similar — non-fatal for UX.
        logger.debug("TUI_IO: /tui edit_text no-op", exc_info=True)
