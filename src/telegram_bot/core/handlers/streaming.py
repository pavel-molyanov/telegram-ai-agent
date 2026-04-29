"""Shared streaming response helper for all handlers."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from aiogram.utils.text_decorations import HtmlDecoration

from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager, StreamEvent
from telegram_bot.core.services.live_buffer import LiveStatusBuffer
from telegram_bot.core.services.providers import choose_available_engine, engine_display_name
from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import StreamMode, TopicConfig
from telegram_bot.core.types import ChannelKey
from telegram_bot.core.types import channel_key as get_channel_key
from telegram_bot.core.utils.telegram_html import (
    _balance_html_tags,
    _markdown_to_html_parts,
    _smart_escape,
    markdown_to_html,
    sanitize_html,
    split_html_message,
)

__all__ = [
    "_balance_html_tags",
    "_markdown_to_html_parts",
    "_smart_escape",
    "build_reply_context",
    "ensure_exec_mode_ready",
    "inject_reply_context",
    "markdown_to_html",
    "resolve_reply_target",
    "sanitize_html",
    "send_streaming_response",
    "send_to_tmux_if_active",
    "split_html_message",
]

# Default when topic_config is not wired in (standalone / legacy tests).
# "verbose" preserves pre-Wave-2 behavior: every status becomes its own message.
_DEFAULT_CALLER_STREAM_MODE: StreamMode = "verbose"

# Per-key lock — see ensure_exec_mode_ready docstring.
_lazy_start_locks: dict[ChannelKey, asyncio.Lock] = {}


def _resolve_stream_mode(
    topic_config: TopicConfig | None,
    channel_key: ChannelKey,
) -> StreamMode:
    """Pick the stream_mode for a channel, falling back to verbose when unknown."""
    if topic_config is None:
        return _DEFAULT_CALLER_STREAM_MODE
    thread_id = channel_key[1]
    return topic_config.get_topic(thread_id).stream_mode


_html_decorator = HtmlDecoration()

_MAX_REPLY_CONTEXT_LEN = 2000


def resolve_reply_target(
    message: Message,
    session_manager: SessionManager,
) -> str | None:
    """Resolve reply-to-resume target from message.reply_to_message.

    Returns target_session_id or None if no reply, no matching session,
    or if the replied-to message belongs to a different channel (cross-topic guard).
    """
    if message.reply_to_message is None:
        return None
    current_channel = get_channel_key(message)
    return session_manager.resolve_reply_session(
        message.reply_to_message.message_id, current_channel
    )


def build_reply_context(message: Message) -> str | None:
    """Extract text from the message being replied to, preserving links.

    Returns formatted text for prompt injection, or None if no reply / no text.
    Used when user replies to a bot message that has no associated session
    (e.g. briefing notifications, task reminders).
    """
    reply = message.reply_to_message
    if reply is None:
        return None
    text = reply.text
    entities = reply.entities
    if text is None:
        text = reply.caption
        entities = reply.caption_entities
    if not text:
        return None
    result = _html_decorator.unparse(text, entities) if entities else text
    if not result:
        return None
    if len(result) > _MAX_REPLY_CONTEXT_LEN:
        result = result[:_MAX_REPLY_CONTEXT_LEN] + t("cc.message_truncated")
    return result


def inject_reply_context(prompt: str, reply_context: str) -> str:
    """Wrap prompt with reply context for agent to see what was replied to."""
    return t("cc.reply_context", context=reply_context, reply=prompt)


async def send_to_tmux_if_active(
    key: ChannelKey,
    prompt: str,
    source_msg: Message,
    tmux_manager: TmuxManager,
) -> bool:
    """Send prompt directly to tmux CC stdin if a tail is active.

    Returns True if dispatched to tmux (caller should return immediately),
    False if not in active tmux tail (caller should enqueue normally).

    Always creates a new "Thinking..." placeholder and rotates the live
    buffer, even when CC is already processing — so status events for
    subsequent prompts appear in a fresh message rather than the original.
    N rapid messages produce N placeholders; each idles until CC reaches it.
    """
    msg_id = source_msg.message_id
    if not (tmux_manager.is_active(key) and tmux_manager.is_tailing(key)):
        logger.info(
            "MSG_TRACE send_to_tmux_if_active skip channel=%s msg=%d active=%s tailing=%s",
            key,
            msg_id,
            tmux_manager.is_active(key),
            tmux_manager.is_tailing(key),
        )
        return False
    logger.info(
        "MSG_TRACE send_to_tmux_if_active dispatch channel=%s msg=%d via=send_direct",
        key,
        msg_id,
    )

    # Resolve stream_mode for this channel from topic_config on tmux_manager
    # (wired at startup). Missing wiring → legacy verbose behavior.
    stream_mode = _resolve_stream_mode(
        tmux_manager.get_topic_config(),  # type: ignore[arg-type]
        key,
    )

    cmd = prompt.split()[0] if prompt.startswith("/") else None
    thinking_text = t("ui.running_command", command=cmd) if cmd else t("ui.thinking")
    thinking_msg = await source_msg.answer(thinking_text, disable_notification=True)

    if stream_mode == "live" and tmux_manager.live_buffer_available():
        bot = tmux_manager.get_live_bot()
        new_buffer = LiveStatusBuffer(
            bot=bot,  # type: ignore[arg-type]
            chat_id=source_msg.chat.id,
            thread_id=key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
        # set_buffer closes the previous buffer atomically, covering the prior thinking page.
        await tmux_manager.set_buffer(key, new_buffer)

    delivered = await tmux_manager.send_direct(key, prompt)
    if not delivered:
        # Modal-blocked or send-keys failure: the thinking placeholder is
        # a lie (CC never received the prompt). Roll back both the
        # placeholder and the LiveStatusBuffer so the user doesn't see
        # an eternal "Thinking..." with no response. send_direct has
        # already posted a modal alert with the pane snapshot; after
        # the user dismisses the modal and resends, a fresh placeholder
        # spawns for that next attempt.
        with contextlib.suppress(TelegramAPIError):
            await thinking_msg.delete()
        await tmux_manager.close_buffer(key)
    return True


logger = logging.getLogger(__name__)


async def ensure_exec_mode_ready(
    key: ChannelKey,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
    source_msg: Message,
) -> bool:
    """Idempotent lazy-start for tmux mode. Returns False only on RuntimeError.

    No-op paths (all return True): tmux already active, or exec_mode != "tmux".
    When exec_mode == "tmux" and tmux is dormant, starts a tmux session using
    the current channel's session blueprint (mode / cwd / mcp_config / chat_id)
    without touching queue or session state — any reset is _reset_channel's
    responsibility.

    On RuntimeError: notifies via source_msg.answer(t("ui.tmux_failed")) and
    returns False. Does NOT touch topic_config — the next user action will
    retry start_session again (no retry-suppression latch by design).
    Non-RuntimeError exceptions from start_session propagate to the caller.

    Per-key asyncio.Lock (`_lazy_start_locks`, module-level, grows monotonically
    by one entry per channel — same pattern as inbox._chat_locks) serializes
    concurrent calls on the same channel so only one start_session actually
    runs; the second caller re-checks is_active inside the critical section
    and returns True.
    """
    msg_id = source_msg.message_id
    lock = _lazy_start_locks.setdefault(key, asyncio.Lock())
    waiting = lock.locked()
    if waiting:
        logger.info(
            "MSG_TRACE ensure_exec_mode_ready waiting_on_lazy_lock channel=%s msg=%d",
            key,
            msg_id,
        )
    async with lock:
        if tmux_manager.is_active(key):
            logger.info(
                "MSG_TRACE ensure_exec_mode_ready already_active channel=%s msg=%d",
                key,
                msg_id,
            )
            return True

        settings = topic_config.get_topic(key[1])
        if settings.exec_mode != "tmux":
            return True

        logger.info(
            "MSG_TRACE ensure_exec_mode_ready start_session_begin channel=%s msg=%d",
            key,
            msg_id,
        )

        current_session = session_manager._get_session(key)
        mode = current_session.mode
        cwd = current_session.cwd
        mcp_config = current_session.mcp_config
        chat_id = current_session.chat_id
        engine = current_session.engine
        model = current_session.model

        thread_id = key[1]
        requested_engine = (
            topic_config.get_topic(thread_id).engine if thread_id is not None else engine
        )
        available_engine = choose_available_engine(requested_engine)
        if available_engine is None:
            await source_msg.answer(t("ui.agent_cli_not_found"))
            return False
        if available_engine != requested_engine:
            logger.warning(
                "Engine %s unavailable for tmux channel %s; falling back to %s",
                requested_engine,
                key,
                available_engine,
            )
            engine = available_engine
            model = None
            current_session.engine = available_engine
            current_session.model = None
            if thread_id is not None:
                ok = await topic_config.update_engine_model(thread_id, available_engine, None)
                if not ok:
                    logger.warning(
                        "Failed to persist tmux fallback engine=%s for thread_id=%s",
                        available_engine,
                        thread_id,
                    )

        # Always spawn fresh — never --resume from peek_saved_session here.
        # Lazy-start-with-resume caused a silent delivery desync in production:
        # the bot tailed a jsonl that no longer received CC's output. Root
        # cause still under investigation (tracked in the internal session-
        # rotation ticket). restore_all and switch_session still use --resume;
        # a proper fix (pid→sessionId pointer via ~/.claude/sessions/<pid>.json)
        # is tracked separately.
        try:
            await tmux_manager.start_session(
                key,
                mode=mode,
                cwd=cwd,
                mcp_config=mcp_config,
                chat_id=chat_id,
                session_manager=session_manager,
                provider=engine,
                model=model,
            )
        except RuntimeError as exc:
            logger.error("Lazy tmux start failed for %s: %s", key, exc)
            await source_msg.answer(t("ui.tmux_failed", exc=exc))
            return False

        logger.info(
            "MSG_TRACE ensure_exec_mode_ready start_session_done channel=%s msg=%d",
            key,
            msg_id,
        )
        await source_msg.answer(
            t("ui.tmux_started_engine", engine=engine_display_name(engine)),
            disable_notification=True,
        )
        return True


@dataclass
class _StreamCtx:
    """Shared state for per-mode on_event handlers.

    Handlers mutate ``send_failed`` and ``accumulated_text`` directly;
    ``sent_message_ids`` is a shared list reference used for bookkeeping.
    Ctx lifetime spans a single ``send_streaming_response`` call — not
    shared across concurrent requests, so no locking is needed.
    """

    message: Message
    channel_key: ChannelKey
    session_manager: SessionManager
    tmux_manager: TmuxManager | None
    stream_mode: StreamMode
    used_tmux: bool
    live_buffer: LiveStatusBuffer | None
    sent_message_ids: list[int]
    accumulated_text: str = ""
    send_failed: bool = False


async def _send_status_silent(ctx: _StreamCtx, content: str) -> None:
    """Send a status event as its own silent message; flip ``send_failed`` on fatal."""
    # html.escape: status strings are plain text (tool names, file paths).
    safe_status = html.escape(content)
    outcome = await send_html_with_fallback(
        send_html=lambda: ctx.message.answer(
            safe_status, parse_mode=ParseMode.HTML, disable_notification=True
        ),
        send_plain=lambda: ctx.message.answer(content, disable_notification=True),
        label=f"status {ctx.channel_key}",
    )
    if outcome.message_id is not None:
        ctx.sent_message_ids.append(outcome.message_id)
    if outcome.fatal:
        ctx.send_failed = True


async def _format_and_send_chunks(
    ctx: _StreamCtx,
    content: str,
    *,
    label: str,
    record_fn: Callable[[int], None] | None = None,
) -> None:
    """Split *content* into HTML chunks and send with plain fallback.

    Short-circuits on the first fatal outcome and flips ``ctx.send_failed``
    so downstream events also bail out. ``record_fn`` (if given) is invoked
    for each successfully-sent chunk — used by the tmux path to record
    message_id → session_id for reply-to-resume. ``split_html_message``
    already runs markdown→HTML + sanitize, so callers pass raw content.
    """
    chunks = split_html_message(content)
    for chunk in chunks:

        async def _send_html(c: str = chunk) -> Any:
            return await ctx.message.answer(c, parse_mode=ParseMode.HTML)

        async def _send_plain(c: str = chunk) -> Any:
            return await ctx.message.answer(c)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=label,
        )
        if outcome.message_id is not None:
            ctx.sent_message_ids.append(outcome.message_id)
            if record_fn is not None:
                record_fn(outcome.message_id)
        if outcome.fatal:
            ctx.send_failed = True
            break


def _record_tmux_message(ctx: _StreamCtx, msg_id: int) -> None:
    """Bind *msg_id* to the current tmux session_id for reply-to-resume.

    Must happen inside ``on_event`` because the tmux tail is long-lived
    (exits only on /cancel, /clear, tmux death, or 6h timeout), so any
    post-stream recording would fire hours after the user's message — if
    ever. Reads session_id and provider from ``tmux_manager`` (live tmux
    state) because ``session_manager``'s copy may carry a stale engine
    after a reply-driven engine switch.
    """
    if not ctx.used_tmux or ctx.tmux_manager is None:
        return
    snapshot = ctx.tmux_manager.get_session_snapshot(ctx.channel_key)
    if snapshot is None:
        return
    sid, provider, model = snapshot
    ctx.session_manager.record_message(msg_id, sid, ctx.channel_key, provider=provider, model=model)


async def _handle_text_event(ctx: _StreamCtx, event: StreamEvent) -> None:
    """Text-event handling shared across all modes.

    Non-tmux: accumulate for the single final message sent after ``send_stream``
    returns. Tmux: the CC TUI transcript has no ``result_message`` event, so
    ``text`` events are the actual CC response — ship each as its own HTML
    message. Multiple blocks (reasoning → tool → text) surface as multiple
    messages; CC TUI emits no end-of-response marker we could batch on.
    """
    if not ctx.used_tmux:
        ctx.accumulated_text += event.content
        return

    await _format_and_send_chunks(
        ctx,
        event.content,
        label=f"text {ctx.channel_key}",
        record_fn=lambda mid: _record_tmux_message(ctx, mid),
    )


async def _handle_result_message_event(ctx: _StreamCtx, event: StreamEvent) -> None:
    """Tmux persistent-mode result-message handling.

    CC with Agent Team emits multiple results per user message — each one
    ships as an immediate formatted message (vs. the non-tmux case where
    a single final response is assembled post-stream). Empty/whitespace
    content is already filtered by the centralized empty guard in on_event.
    """
    await _format_and_send_chunks(
        ctx,
        event.content,
        label=f"result_message {ctx.channel_key}",
        record_fn=lambda mid: _record_tmux_message(ctx, mid),
    )


async def _handle_event_verbose(ctx: _StreamCtx, event: StreamEvent) -> None:
    """verbose-mode: every status is a silent message; text/result as usual."""
    if event.type == "status":
        await _send_status_silent(ctx, event.content)
    elif event.type == "text":
        await _handle_text_event(ctx, event)
    elif event.type == "result_message":
        await _handle_result_message_event(ctx, event)


async def _handle_event_live(ctx: _StreamCtx, event: StreamEvent) -> None:
    """live-mode fallthrough: status without a buffer behaves like verbose.

    Status events that landed in an editable buffer are consumed BEFORE
    dispatch (see ``_live_append_status`` in ``send_streaming_response``);
    if we see a status here the buffer was unavailable (no bot, tmux
    buffer unset) and we fall back to silent messages so the user still
    sees progress.
    """
    if event.type == "status":
        await _send_status_silent(ctx, event.content)
    elif event.type == "text":
        await _handle_text_event(ctx, event)
    elif event.type == "result_message":
        await _handle_result_message_event(ctx, event)


async def _handle_event_minimal(ctx: _StreamCtx, event: StreamEvent) -> None:
    """minimal-mode: status already dropped; text/result behave normally.

    Status never reaches this handler — filtered in the dispatcher above.
    The branch is omitted so a future dispatcher bug surfaces as a silent
    drop rather than an unexpected status message flood.
    """
    if event.type == "text":
        await _handle_text_event(ctx, event)
    elif event.type == "result_message":
        await _handle_result_message_event(ctx, event)


async def _send_final_response(ctx: _StreamCtx, final_text: str) -> None:
    """Send the concluding response with the topic keyboard (groups only).

    Records response message_ids under the current session for
    reply-to-resume. Status/text/result messages sent during streaming are
    NOT recorded here — users reply to the final answer, not to intermediate
    progress updates. Aborts early if a prior handler already flipped
    ``send_failed`` (Telegram clearly rejecting everything).
    """
    # Supergroup forum: topic_keyboard (3 buttons in a row)
    # Private chat: no keyboard (buttons would go to General topic)
    is_group = ctx.message.chat.type == ChatType.SUPERGROUP
    reply_kb = topic_keyboard() if is_group else None

    chunks = split_html_message(final_text)
    response_message_ids: list[int] = []
    for chunk in chunks:
        if ctx.send_failed:
            break

        async def _send_html(c: str = chunk) -> Any:
            return await ctx.message.answer(c, parse_mode=ParseMode.HTML, reply_markup=reply_kb)

        async def _send_plain(c: str = chunk) -> Any:
            return await ctx.message.answer(c, reply_markup=reply_kb)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"final_chunk {ctx.channel_key}",
            flood_retry_limit=300.0,
        )
        if outcome.message_id is not None:
            response_message_ids.append(outcome.message_id)
        if outcome.fatal:
            ctx.send_failed = True

    # Record only final response message IDs for reply-to-resume
    # (users reply to responses, not intermediate status messages).
    current_sid = ctx.session_manager.get_current_session_id(ctx.channel_key)
    if current_sid:
        for msg_id in response_message_ids:
            ctx.session_manager.record_message(msg_id, current_sid, ctx.channel_key)


async def send_streaming_response(
    message: Message,
    session_manager: SessionManager,
    channel_key: ChannelKey,
    prompt: str,
    git_sync: Any | None = None,
    tmux_manager: TmuxManager | None = None,
    topic_config: TopicConfig | None = None,
) -> None:
    """Send prompt to CC with streaming and deliver response to user.

    stream_mode controls what reaches Telegram between the thinking placeholder
    and the final result:
      verbose — every status event ships as its own silent message (legacy).
      minimal — status events dropped; only the thinking + results stay,
                which is what project topics want so agent-team chatter
                doesn't hit the SendMessage flood limit.
      live    — status events are appended to a single editable
                ``LiveStatusBuffer`` message; falls back to verbose behaviour
                for status when no buffer is available.
    All message IDs are still recorded for reply-to-resume.
    """
    stream_mode = _resolve_stream_mode(topic_config, channel_key)
    # User-content preview — DEBUG only to keep INFO journalctl clean of PII.
    logger.debug(
        "Prompt to CC (channel %s, stream_mode=%s): %.200s",
        channel_key,
        stream_mode,
        prompt,
    )

    sent_message_ids: list[int] = []

    cmd = prompt.split()[0] if prompt.startswith("/") else None
    thinking_text = t("ui.running_command", command=cmd) if cmd else t("ui.thinking")
    thinking_msg = await message.answer(thinking_text, disable_notification=True)
    sent_message_ids.append(thinking_msg.message_id)

    used_tmux = tmux_manager is not None and tmux_manager.is_active(channel_key)

    # Materialize a LiveStatusBuffer for live-mode. For tmux it's registered
    # on the manager so on_event (which may fire from a long-running tail)
    # can always look up the current buffer. For non-tmux the buffer lives
    # in this function's closure and is closed in finally.
    live_buffer: LiveStatusBuffer | None = None
    if (
        stream_mode == "live"
        and message.bot is not None
        and not used_tmux  # tmux case wires a fresh buffer below
    ):
        live_buffer = LiveStatusBuffer(
            bot=message.bot,
            chat_id=message.chat.id,
            thread_id=channel_key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
    if (
        stream_mode == "live"
        and used_tmux
        and tmux_manager is not None
        and tmux_manager.live_buffer_available()
        and message.bot is not None
    ):
        bot = tmux_manager.get_live_bot()
        tmux_buffer = LiveStatusBuffer(
            bot=bot,  # type: ignore[arg-type]
            chat_id=message.chat.id,
            thread_id=channel_key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
        await tmux_manager.set_buffer(channel_key, tmux_buffer)

    ctx = _StreamCtx(
        message=message,
        channel_key=channel_key,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        stream_mode=stream_mode,
        used_tmux=used_tmux,
        live_buffer=live_buffer,
        sent_message_ids=sent_message_ids,
    )

    async def _live_append_status(event: StreamEvent) -> bool:
        """Append a status event to the live buffer, if one is active.

        Only status events (tool-call progress lines) go to the buffer —
        text events are routed through ``_handle_text_event`` because the
        HTML-mode buffer uses ``html.escape`` which would mangle CC's
        markdown output. Re-reads the tmux buffer on every call so a
        mid-stream rotation (new user message) picks up the fresh one.

        Returns True iff the event landed in a buffer and must NOT be
        forwarded to the mode dispatcher.
        """
        if ctx.stream_mode != "live" or event.type != "status":
            return False
        buf: LiveStatusBuffer | None
        if ctx.used_tmux and ctx.tmux_manager is not None:
            raw = ctx.tmux_manager.get_buffer(ctx.channel_key)
            buf = raw if isinstance(raw, LiveStatusBuffer) else None
        else:
            buf = ctx.live_buffer
        if buf is None:
            return False
        # html.escape: status strings are plain text generated by our tool-status
        # mapper — not CC markdown output.  We want literal display of any <, >, &
        # in file paths or tool arguments, so use stdlib escape, not sanitize_html
        # (which would restore Telegram-allowed tag names like <b> back to HTML).
        await buf.append(html.escape(event.content))
        return True

    async def on_event(event: StreamEvent) -> None:
        # send_failed latches across subsequent events: stop sending to
        # Telegram, but keep accumulating non-tmux text so the final
        # summary still assembles if the retry policy eventually recovers.
        if ctx.send_failed:
            if event.type == "text" and not ctx.used_tmux:
                ctx.accumulated_text += event.content
            return

        # Central empty-content guard (W1.2). CC emits empty events at
        # compact boundaries, token-count-only events, and empty thinking
        # blocks. Forwarding those to Telegram fails — split_html_message
        # on "" yields [""], then message.answer("") → TelegramBadRequest.
        # Per-mode handlers receive only non-empty events.
        if event.type in ("status", "text", "result_message") and not event.content.strip():
            logger.debug("Dropping empty %s event on channel %s", event.type, ctx.channel_key)
            return

        # Mode-specific early drops / routing done before dispatch so the
        # per-mode handlers stay flat and uniform.
        if ctx.stream_mode == "minimal" and event.type == "status":
            return
        if await _live_append_status(event):
            return

        match ctx.stream_mode:
            case "live":
                await _handle_event_live(ctx, event)
            case "verbose":
                await _handle_event_verbose(ctx, event)
            case "minimal":
                await _handle_event_minimal(ctx, event)
            case _:  # defensive: unknown mode shouldn't silently drop events
                logger.warning(
                    "Unknown stream_mode %r on channel %s; falling back to verbose",
                    ctx.stream_mode,
                    ctx.channel_key,
                )
                await _handle_event_verbose(ctx, event)

    try:
        if used_tmux:
            assert tmux_manager is not None
            response = await tmux_manager.send_stream(channel_key, prompt, on_event)
            # Sync session_id back so reply-to-resume works
            new_sid = tmux_manager.get_session_id(channel_key)
            if new_sid:
                await session_manager.override_session(channel_key, new_sid)
        else:
            response = await session_manager.send_stream(channel_key, prompt, on_event)
    except asyncio.CancelledError:
        # Status messages ARE the history — no cleanup needed
        raise
    finally:
        # Close the non-tmux live buffer if we owned one. In tmux mode the
        # buffer is owned by tmux_manager and stays alive across the tail —
        # it's closed when the next user message arrives or on /clear.
        if live_buffer is not None:
            with contextlib.suppress(Exception):
                await live_buffer.close()
            # Absorb per-page message IDs so reply-to-resume covers every page.
            for mid in live_buffer.message_ids:
                if mid not in sent_message_ids:
                    sent_message_ids.append(mid)

    # Notify git sync for knowledge mode (fire-and-forget)
    if git_sync is not None:
        mode = session_manager.get_mode(channel_key)
        if mode == "knowledge":
            try:
                git_sync.notify()
            except RuntimeError:
                logger.debug("Git sync notify skipped, event loop closing")

    # In tmux mode, results are sent immediately via result_message events.
    # Don't use accumulated_text as fallback — it spans multiple interactions
    # and would dump hours of output as one message on cancel.
    # In tmux each result_message is recorded inside on_event (long-lived tail),
    # so no post-stream recording is needed here.
    final_text = response or (ctx.accumulated_text if not used_tmux else "")
    if not final_text:
        return

    await _send_final_response(ctx, final_text)
