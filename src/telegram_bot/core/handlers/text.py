"""Text message handler — forwards text to Claude Code via MessageQueue."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.types import Message

from telegram_bot.core.handlers._dispatch import enqueue_prompt
from telegram_bot.core.handlers.forward import ForwardBatcher, unparse_entities
from telegram_bot.core.handlers.streaming import (
    ensure_exec_mode_ready,
    send_to_tmux_if_active,
)
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.providers import engine_display_name
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.tui.routing import route_slash_command
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)

router = Router(name="text")


@router.message(F.text)
async def handle_text(
    message: Message,
    session_manager: SessionManager,
    forward_batcher: ForwardBatcher,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    inbox_reply_handler: Callable[[Message, MessageQueue], Awaitable[bool]] | None = None,
) -> None:
    # User's own messages are trusted — no sanitize_forwarded_content() needed here.
    # If multi-user access is added, apply sanitization like in forward.py.
    text = unparse_entities(message.text, message.entities)
    if not text.strip():
        return

    key = channel_key(message)
    logger.info(
        "MSG_TRACE handle_text channel=%s msg=%d text_len=%d user=%s",
        key,
        message.message_id,
        len(text),
        message.from_user and message.from_user.id,
    )

    if inbox_reply_handler is not None and await inbox_reply_handler(message, message_queue):
        return

    # Slash-command forwarding for tmux topics (Decision 9, Wave 3).
    # Non-whitelist slash commands (`/model`, `/compact`, `/mcp`, …) go
    # directly to the CC TUI via send-keys, bypassing the forward batcher
    # and MessageQueue. Bot-reserved commands from BOT_RESERVED_COMMANDS
    # (`/new`, `/kill`, `/cancel`, …) never reach this handler — aiogram
    # routes them by Command() filter first. Reply-to-resume is not applied:
    # slash commands logically belong to the currently-live TUI, not to
    # whatever session the reply-target references.
    if (
        text.startswith("/")
        and route_slash_command(text) == "tui"
        and topic_config.get_topic(key[1]).exec_mode == "tmux"
    ):
        if not await ensure_exec_mode_ready(
            key, topic_config, tmux_manager, session_manager, message
        ):
            return
        if await send_to_tmux_if_active(key, text, message, tmux_manager):
            return

    async def on_text_only(text: str, source_msg: Message) -> None:
        """Called when text message has no accompanying forwards."""
        reply_ref = None
        if source_msg.reply_to_message is not None:
            reply_ref = session_manager.resolve_reply_reference(
                source_msg.reply_to_message.message_id,
                key,
            )

        if reply_ref is not None:
            settings = topic_config.get_topic(key[1])
            if reply_ref.provider not in {"claude", "codex"}:
                await source_msg.answer(t("ui.tui_session_missing"))
                return
            target_exec_mode = reply_ref.exec_mode
            if target_exec_mode is not None and target_exec_mode not in {
                "subprocess",
                "tmux",
            }:
                await source_msg.answer(t("ui.tui_session_missing"))
                return
            provider_changed = reply_ref.provider != settings.engine
            exec_mode_changed = (
                target_exec_mode is not None and target_exec_mode != settings.exec_mode
            )
            if provider_changed or exec_mode_changed:
                if tmux_manager.is_processing(key) or message_queue.is_busy(key):
                    await source_msg.answer(t("ui.exec_mode_busy"))
                    return
                thread_id = key[1]
                if thread_id is None:
                    await source_msg.answer(t("ui.engine_not_in_forum"))
                    return
                if exec_mode_changed and provider_changed:
                    assert target_exec_mode is not None
                    ok = await topic_config.update_engine_model_exec_mode(
                        thread_id,
                        reply_ref.provider,  # type: ignore[arg-type]
                        None,
                        target_exec_mode,
                    )
                    if not ok:
                        await source_msg.answer(t("ui.engine_write_failed"))
                        return
                elif exec_mode_changed:
                    assert target_exec_mode is not None
                    ok = await topic_config.update_exec_mode(thread_id, target_exec_mode)
                    if not ok:
                        await source_msg.answer(t("ui.exec_mode_write_failed"))
                        return
                elif provider_changed:
                    ok = await topic_config.update_engine_model(
                        thread_id,
                        reply_ref.provider,  # type: ignore[arg-type]
                        None,
                    )
                    if not ok:
                        await source_msg.answer(t("ui.engine_write_failed"))
                        return
                if tmux_manager.is_active(key) and (
                    provider_changed or reply_ref.exec_mode == "subprocess"
                ):
                    await tmux_manager.kill(key)
                if provider_changed:
                    await session_manager.clear_provider_session(key, mark_fresh=False)
                logger.info(
                    "reply switched context for %s: %s/%s/%s -> %s/%s/%s",
                    key,
                    settings.engine,
                    settings.model,
                    settings.exec_mode,
                    reply_ref.provider,
                    reply_ref.model,
                    reply_ref.exec_mode or settings.exec_mode,
                )
                if provider_changed:
                    await source_msg.answer(
                        t(
                            "ui.reply_engine_switched",
                            engine=engine_display_name(reply_ref.provider),
                        )
                    )

        if not await ensure_exec_mode_ready(
            key, topic_config, tmux_manager, session_manager, source_msg
        ):
            return

        target_session_id = reply_ref.session_id if reply_ref is not None else None
        tmux_switched = False

        # Tmux mode: switch CC session if reply targets a different one.
        # Prior session context is preserved on disk; both sessions remain resumable.
        if tmux_manager.is_active(key) and target_session_id:
            assert reply_ref is not None
            current_sid = tmux_manager.get_session_id(key)
            if current_sid != target_session_id:
                # switch_session returns False when the target session is not
                # registered in tmux state (e.g. created before the tui-v1
                # migration, so build_tmux_startup_args does not know it).
                # Surface ui.tui_session_missing and stop — do NOT fall through
                # to send_to_tmux_if_active / enqueue under the current session.
                ok = await tmux_manager.switch_session(key, target_session_id, session_manager)
                if not ok:
                    await source_msg.answer(t("ui.tui_session_missing"))
                    return
                tmux_switched = True
                await source_msg.answer(
                    t(
                        "ui.session_switched_engine",
                        engine=engine_display_name(reply_ref.provider),
                        sid=target_session_id[:8],
                    )
                )
            target_session_id = None  # tmux manages session state internally

        # Tmux with active tail: send directly to CC stdin, bypass queue.
        if await send_to_tmux_if_active(key, text, source_msg, tmux_manager):
            return

        enqueue_prompt(
            key,
            text,
            source_msg,
            message_queue,
            tmux_manager,
            target_session_id=target_session_id,
            # tmux_switched already consumed the reply target in switch_session;
            # a second reply-context injection would double-reference it.
            inject_reply_if_no_target=not tmux_switched,
        )

    # Add to batcher — will wait for forwards or process alone after debounce
    forward_batcher.add_text(key, text, message, on_text_only)
