"""Command handlers for bot-owned slash commands except /tui and /tail."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import os
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.types.inaccessible_message import InaccessibleMessage

from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.keyboards import (
    RESUME_PAGE_SIZE,
    _format_age,
    _format_size,
    engine_keyboard,
    exec_mode_keyboard,
    resume_keyboard,
    stream_mode_keyboard,
    topic_keyboard,
)
from telegram_bot.core.messages import reset_lang_cache, t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.picker_store import PickerState, PickerStore
from telegram_bot.core.services.providers import engine_display_name
from telegram_bot.core.services.resume_listing import (
    SessionEntry,
    _same_cwd,
    get_last_assistant_message,
    list_sessions,
)
from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import (
    _VALID_ENGINES,
    _VALID_EXEC_MODES,
    _VALID_STREAM_MODES,
    TopicConfig,
)
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey, channel_key
from telegram_bot.core.utils.telegram_html import split_html_message

logger = logging.getLogger(__name__)


def _exec_mode_label(mode: str) -> str:
    """Human-facing label per exec_mode.

    Raw "subprocess" must never leak into the "Mode: …" toast — the picker
    button text is the contract surface.
    """
    if mode == "subprocess":
        return t("ui.exec_mode_label_subprocess")
    if mode == "tmux":
        return t("ui.exec_mode_label_tmux")
    return mode


def _exec_mode_picker_caption(mode: str) -> str:
    return t("ui.exec_mode_picker_caption", current=_exec_mode_label(mode))


router = Router(name="commands")


def _resume_caption(
    cwd: Path,
    *,
    page: int,
    total_pages: int,
    entries: tuple[SessionEntry, ...] = (),
    current_session_id: str | None = None,
) -> str:
    safe_cwd = html.escape(str(cwd))
    text = t("ui.resume_picker_caption_hdr", cwd=safe_cwd, page=page + 1, total=total_pages)
    if not entries:
        return text

    blocks: list[str] = []
    start = page * RESUME_PAGE_SIZE
    for idx, entry in enumerate(entries[start : start + RESUME_PAGE_SIZE], start=start):
        provider = engine_display_name(entry.provider)
        preview = html.escape(entry.preview)
        prefix = "✅ " if entry.session_id == current_session_id else ""
        parts = [
            f"{prefix}{idx + 1}. <b>{provider}</b>",
            _format_age(entry.mtime),
            _format_size(entry.size_bytes),
            f"<code>{html.escape(entry.session_id[:8])}</code>",
        ]
        if entry.session_id == current_session_id:
            parts.append(t("ui.resume_current_marker"))
        block_lines = [" · ".join(parts)]
        if preview:
            block_lines.append(f"   {preview}")
        blocks.append("\n".join(block_lines))
    return "\n\n".join([text, *blocks])


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    logger.debug("User %s started the bot", message.from_user and message.from_user.id)
    is_group = message.chat.type == ChatType.SUPERGROUP
    keyboard = topic_keyboard() if is_group else ReplyKeyboardRemove()
    await message.answer(
        text=t("ui.start_welcome"),
        reply_markup=keyboard,
    )


@router.message(Command("language"))
async def handle_language(message: Message) -> None:
    """Show or switch bot UI language for the current process."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    current = os.environ.get("BOT_LANG", "en")
    if current not in {"en", "ru"}:
        current = "en"

    if len(parts) == 1:
        await message.answer(t("ui.language_current", lang=current))
        return

    lang = parts[1].strip().lower()
    if lang not in {"en", "ru"}:
        await message.answer(t("ui.language_invalid"))
        return

    os.environ["BOT_LANG"] = lang
    reset_lang_cache()
    await message.answer(t("ui.language_changed", lang=lang))


async def _reset_channel(
    message: Message,
    key: ChannelKey,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Unified reset path for /new, /clear, and the "Новый чат" reply button.

    Live tmux → clear_context respawns a fresh TUI immediately.
    Dormant tmux → drop stale state and start a fresh TUI immediately.
    Otherwise → full subprocess reset + ui.new_session.
    """
    settings = topic_config.get_topic(key[1])
    if tmux_manager.is_active(key):
        # clear_context respawns the tmux session; _spawn_tmux can fail
        # (tmux server shutdown race, readiness timeout, etc.). Without a
        # catch here the RuntimeError reaches aiogram's error middleware
        # and the user sees nothing — "Новый чат" becomes a silent button.
        try:
            reset_live = await tmux_manager.clear_context(key, session_manager)
        except RuntimeError:
            logger.warning("clear_context failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        if reset_live:
            session = session_manager._get_session(key)
            await message.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
            )
            return
        logger.info("clear_context found no live tmux for %s; starting fresh", key)

    if settings.exec_mode == "tmux":
        await tmux_manager.kill(key)
        forward_batcher.clear(key)
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        session = session_manager._get_session(key)
        try:
            started = await tmux_manager.start_session(
                key,
                mode=session.mode,
                cwd=session.cwd,
                mcp_config=session.mcp_config,
                chat_id=session.chat_id,
                session_manager=session_manager,
                provider=session.engine,
                model=session.model,
            )
        except RuntimeError:
            logger.warning("fresh tmux start failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        if started:
            await message.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
            )
        return

    forward_batcher.clear(key)
    await message_queue.clear(key)
    await session_manager.kill_session(key)
    await message.answer(t("ui.new_session"))


@router.message(Command("new"))
async def handle_new(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested new session", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("clear"))
async def handle_clear(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested clear", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
) -> None:
    key = channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    cancelled = await message_queue.cancel(key)
    if cancelled or tmux_acted:
        logger.debug("User cancelled CC processing (command) for %s", key)
        await message.answer(t("ui.cancelled"))
    else:
        await message.answer(t("ui.nothing_to_cancel"))


@router.message(Command("kill"))
async def handle_kill(message: Message, tmux_manager: TmuxManager) -> None:
    """Kill the tmux session in the current topic."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tmux_not_active"))
        return
    logger.debug(
        "User %s killed tmux session for %s", message.from_user and message.from_user.id, key
    )
    await tmux_manager.kill(key)
    await message.answer(t("ui.tmux_killed"))


@router.message(Command("mcpstatus"))
async def handle_mcpstatus(message: Message, tmux_manager: TmuxManager) -> None:
    """Show redacted MCP process diagnostics for the current topic."""
    key = channel_key(message)
    status = html.escape(tmux_manager.mcp_status_text(key))
    await message.answer(f"<pre>{status}</pre>", parse_mode="HTML")


@router.message(Command("recycle"))
async def handle_recycle(
    message: Message,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
    message_queue: MessageQueue,
) -> None:
    """Restart the current tmux runtime without intentionally clearing context."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tmux_not_active"))
        return
    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await message.answer(t("ui.exec_mode_busy"))
        return
    try:
        ok = await tmux_manager.recycle(key, session_manager)
    except RuntimeError:
        logger.warning("recycle failed for %s", key, exc_info=True)
        await message.answer(t("ui.recycle_failed"))
        return
    if ok:
        await message.answer(t("ui.recycle_done"))
    else:
        await message.answer(t("ui.tmux_not_active"))


@router.message(Command("resume"))
async def handle_resume(
    message: Message,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    """Open server-side picker with resumable Claude/Codex sessions."""
    key = channel_key(message)
    if key[1] is None:
        await message.answer(t("ui.resume_not_in_forum"))
        return

    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    entries = tuple(await asyncio.to_thread(list_sessions, runtime.cwd))
    if not entries:
        await message.answer(t("ui.resume_no_sessions"))
        return

    token = picker_store.put(
        PickerState(
            chat_id=key[0],
            thread_id=key[1],
            cwd=runtime.cwd,
            engine=runtime.engine,
            entries=entries,
            created_at=time.time(),
        )
    )
    total_pages = max(1, math.ceil(len(entries) / 8))
    current_session_id = tmux_manager.get_active_session_id(key)
    await message.answer(
        _resume_caption(
            runtime.cwd,
            page=0,
            total_pages=total_pages,
            entries=entries,
            current_session_id=current_session_id,
        ),
        reply_markup=resume_keyboard(
            entries,
            page=0,
            current_session_id=current_session_id,
            token=token,
        ),
        parse_mode="HTML",
    )


def _callback_key(callback: CallbackQuery) -> ChannelKey | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return (callback.message.chat.id, callback.message.message_thread_id)


async def _stale_resume_picker(callback: CallbackQuery) -> None:
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_picker_stale"), reply_markup=None)
    await callback.answer(t("ui.resume_picker_stale"), show_alert=True)


async def _answer_callback_safely(
    callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer(text, show_alert=show_alert)


async def _replay_last_assistant_message(
    message: Message,
    entry: SessionEntry,
    key: ChannelKey,
    session_manager: SessionManager,
) -> None:
    content = await asyncio.to_thread(
        get_last_assistant_message,
        entry.provider,
        entry.transcript_path,
    )
    if not content:
        return

    for chunk in split_html_message(content):

        async def _send_html(c: str = chunk) -> object:
            return await message.answer(c, parse_mode="HTML")

        async def _send_plain(c: str = chunk) -> object:
            return await message.answer(c)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"resume replay {key}",
        )
        if outcome.message_id is not None:
            session_manager.record_message(
                outcome.message_id,
                entry.session_id,
                key,
                provider=entry.provider,
                model=None,
            )
        if outcome.fatal:
            return


@router.callback_query(F.data.startswith("rs:p:"))
async def on_resume_page(
    callback: CallbackQuery,
    picker_store: PickerStore,
    tmux_manager: TmuxManager,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_page = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    try:
        page = int(raw_page)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    total_pages = max(1, math.ceil(len(state.entries) / 8))
    page = max(0, min(page, total_pages - 1))
    try:
        await callback.message.edit_text(
            _resume_caption(
                state.cwd,
                page=page,
                total_pages=total_pages,
                entries=state.entries,
                current_session_id=tmux_manager.get_active_session_id(key),
            ),
            reply_markup=resume_keyboard(
                state.entries,
                page=page,
                current_session_id=tmux_manager.get_active_session_id(key),
                token=token,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("rs:s:"))
async def on_resume_pick(
    callback: CallbackQuery,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_idx = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    if not _same_cwd(runtime.cwd, state.cwd):
        await _stale_resume_picker(callback)
        return
    try:
        idx = int(raw_idx)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    if idx < 0:
        await _stale_resume_picker(callback)
        return
    try:
        entry = state.entries[idx]
    except IndexError:
        await _stale_resume_picker(callback)
        return

    await _answer_callback_safely(callback, t("ui.resume_starting"))
    result = await tmux_manager.switch_or_start_session(
        key,
        entry.session_id,
        entry.provider,
        entry.transcript_path,
        session_manager=session_manager,
        topic_config=topic_config,
        defaults=bot_defaults,
    )
    if result.kind == "target_missing":
        await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
        return
    if result.kind in {"invalid_id", "spawn_failed", "config_write_failed"}:
        key_name = (
            "ui.resume_spawn_failed_engine_changed"
            if result.kind == "spawn_failed" and result.engine_changed
            else f"ui.resume_{result.kind}"
        )
        await callback.message.edit_text(
            t(key_name, engine=entry.provider),
            reply_markup=None,
        )
        return

    picker_store.drop(token)
    if result.kind == "already_on_it":
        await callback.message.edit_text(t("ui.resume_already_on_it"), reply_markup=None)
        await _replay_last_assistant_message(callback.message, entry, key, session_manager)
        return

    message_key = "ui.resume_switched" if result.kind == "switched" else "ui.resume_started"
    text = t(message_key, sid=entry.session_id[:8])
    if result.engine_changed:
        text += "\n" + t("ui.resume_engine_switched", engine=entry.provider)
    await callback.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    await _replay_last_assistant_message(callback.message, entry, key, session_manager)


@router.callback_query(F.data.startswith("rs:cancel:"))
async def on_resume_cancel(callback: CallbackQuery, picker_store: PickerStore) -> None:
    if callback.data is not None:
        picker_store.drop(callback.data.rsplit(":", 1)[-1])
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_cancelled"), reply_markup=None)
    await callback.answer()


@router.message(Command("stream"))
async def handle_stream_mode(message: Message, topic_config: TopicConfig) -> None:
    """Show a 3-button picker to switch stream_mode for the current topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.stream_mode_not_in_forum"))
        return
    current = topic_config.get_topic(thread_id).stream_mode
    await message.answer(
        t("ui.stream_mode_picker_caption", current=current),
        reply_markup=stream_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("stream_mode:"))
async def on_stream_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
) -> None:
    """Apply a new stream_mode for the topic the picker was posted in."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id/edit methods — bail out if the
    # picker message is no longer reachable (e.g. deleted, chat lost).
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, mode = callback.data.partition(":")
    if mode not in _VALID_STREAM_MODES:
        await callback.answer(t("ui.stream_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(
            t("ui.stream_mode_not_in_forum"),
            show_alert=True,
        )
        return

    ok = await topic_config.update_stream_mode(thread_id, mode)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.stream_mode_write_failed"), show_alert=True)
        return

    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            t("ui.stream_mode_picker_caption", current=mode),
            reply_markup=stream_mode_keyboard(mode),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh stream_mode picker", exc_info=True)
    await callback.answer(t("ui.stream_mode_changed", mode=mode))


@router.message(Command("mode"))
async def handle_mode_command(message: Message, topic_config: TopicConfig) -> None:
    """Show a 2-button picker to switch exec_mode for the current topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.exec_mode_not_in_forum"))
        return
    current = topic_config.get_topic(thread_id).exec_mode
    await message.answer(
        _exec_mode_picker_caption(current),
        reply_markup=exec_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("exec_mode:"))
async def on_exec_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
) -> None:
    """Apply a new exec_mode for the topic the picker was posted in.

    Order matters: busy-check precedes any side-effect, and tmux.kill strictly
    precedes the config write on tmux→subprocess (Decision 2 — if we wrote
    first and crashed, the next message would race a still-running tmux
    session against a fresh subprocess under the new mode).
    """
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id / edit methods — bail out if the
    # picker message is no longer reachable.
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, new_mode = callback.data.partition(":")
    # Re-validate against the whitelist even though the keyboard only emits
    # two canonical values — raw callback.data is user-controlled.
    if new_mode not in _VALID_EXEC_MODES:
        await callback.answer(t("ui.exec_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.exec_mode_not_in_forum"), show_alert=True)
        return

    key = (callback.message.chat.id, thread_id)
    previous_mode = topic_config.get_topic(thread_id).exec_mode

    if new_mode == previous_mode:
        await callback.answer(t("ui.exec_mode_already", mode=_exec_mode_label(new_mode)))
        return

    # Busy-check covers both channels: tmux's own processing flag AND the
    # subprocess-path MessageQueue (lock held OR items pending). Either way
    # we refuse the switch without touching tmux state.
    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    # tmux→subprocess: kill first, then persist. Reverse order leaves an
    # orphan tmux session if the write fails.
    if previous_mode == "tmux" and new_mode == "subprocess":
        await tmux_manager.kill(key)

    ok = await topic_config.update_exec_mode(thread_id, new_mode)
    if not ok:
        await callback.answer(t("ui.exec_mode_write_failed"), show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else None
    logger.info(
        "exec_mode switched: user_id=%s thread_id=%s previous_mode=%s new_mode=%s",
        user_id,
        thread_id,
        previous_mode,
        new_mode,
    )

    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            _exec_mode_picker_caption(new_mode),
            reply_markup=exec_mode_keyboard(new_mode),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh exec_mode picker", exc_info=True)
    await callback.answer(t("ui.exec_mode_changed", mode=_exec_mode_label(new_mode)))


@router.message(Command("engine"))
async def handle_engine_command(message: Message, topic_config: TopicConfig) -> None:
    """Show provider engine picker for the current forum topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.engine_not_in_forum"))
        return
    settings = topic_config.get_topic(thread_id)
    await message.answer(
        t(
            "ui.engine_picker_caption",
            engine=engine_display_name(settings.engine),
        ),
        reply_markup=engine_keyboard(settings.engine),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("engine:"))
async def on_engine_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
) -> None:
    """Apply provider engine changes for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, raw_value = callback.data.partition(":")
    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.engine_not_in_forum"), show_alert=True)
        return
    key = (callback.message.chat.id, thread_id)
    current = topic_config.get_topic(thread_id)

    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    if raw_value not in _VALID_ENGINES:
        await callback.answer(t("ui.engine_invalid"), show_alert=True)
        return
    new_engine = raw_value

    if new_engine == current.engine:
        await callback.answer(t("ui.engine_already"))
        return

    ok = await topic_config.update_engine_model(thread_id, new_engine, None)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.engine_write_failed"), show_alert=True)
        return

    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    await session_manager.clear_provider_session(key)

    logger.info(
        "engine switched: user_id=%s thread_id=%s previous=%s new=%s model=%s",
        callback.from_user.id if callback.from_user else None,
        thread_id,
        current.engine,
        new_engine,
        current.model,
    )
    engine_name = engine_display_name(new_engine)
    try:
        await callback.message.edit_text(
            t("ui.engine_picker_caption", engine=engine_name),
            reply_markup=engine_keyboard(new_engine),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh engine picker", exc_info=True)
    await callback.answer(t("ui.engine_changed", engine=engine_name))
    await callback.message.answer(t("ui.engine_changed_new_session", engine=engine_name))
