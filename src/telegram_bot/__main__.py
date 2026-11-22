"""Entry point for the public Telegram-Claude-Code bot."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from telegram_bot.core.config import get_settings
from telegram_bot.core.handlers.cancel import router as cancel_router
from telegram_bot.core.handlers.commands import router as commands_router
from telegram_bot.core.handlers.forum_topic import router as forum_topic_router
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.handlers.forward import router as forward_router
from telegram_bot.core.handlers.mode import router as mode_router
from telegram_bot.core.handlers.photo import cleanup_old_tmp_files, ensure_tmp_dir
from telegram_bot.core.handlers.photo import router as photo_router
from telegram_bot.core.handlers.recovery import make_recovery_on_event
from telegram_bot.core.handlers.streaming import send_streaming_response
from telegram_bot.core.handlers.tail import router as tail_router
from telegram_bot.core.handlers.text import router as text_router
from telegram_bot.core.handlers.voice import router as voice_router
from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.bot_commands import setup_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.codex_update import CodexUpdateService
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.picker_store import PickerStore
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


_PUBLIC_BOT_SECRET_ENV = {"TELEGRAM_BOT_TOKEN", "DEEPGRAM_API_KEY"}


def _migrate_legacy_default_tmux_server(
    tmux_sessions_dir: Path,
    marker: Path,
) -> None:
    """Remove state-owned legacy panes and credentials from the shared server."""
    if marker.exists():
        return
    state_path = tmux_sessions_dir / "state.json"
    session_names: set[str] = set()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict) and isinstance(entry.get("session_name"), str):
                session_names.add(entry["session_name"])

    legacy_env = dict(os.environ)
    legacy_env.pop("TMUX_TMPDIR", None)
    legacy_env.pop("TMUX", None)
    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
        env=legacy_env,
    )
    if listed.returncode == 0:
        existing = set(listed.stdout.splitlines())
        for name in sorted(session_names & existing):
            killed = subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                capture_output=True,
                check=False,
                env=legacy_env,
            )
            if killed.returncode != 0:
                raise RuntimeError(f"Failed to migrate legacy tmux session {name!r}")

        if existing - session_names:
            for key in sorted(_PUBLIC_BOT_SECRET_ENV & set(legacy_env)):
                scrubbed = subprocess.run(
                    ["tmux", "set-environment", "-gu", key],
                    capture_output=True,
                    check=False,
                    env=legacy_env,
                )
                if scrubbed.returncode != 0:
                    raise RuntimeError(f"Failed to scrub legacy tmux environment key {key!r}")

    marker.write_text("migrated\n", encoding="utf-8")
    marker.chmod(0o600)


def _ensure_dedicated_tmux_tmpdir(workspace_root: Path, tmux_sessions_dir: Path) -> Path:
    """Keep bot sessions off the operator's default tmux server."""
    configured = os.environ.get("TMUX_TMPDIR")
    runtime_dir = Path(configured) if configured else workspace_root / ".telegram-bot-tmux"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_dir.chmod(0o700)
    if not configured:
        _migrate_legacy_default_tmux_server(
            tmux_sessions_dir,
            runtime_dir / ".legacy-default-migrated",
        )
    os.environ["TMUX_TMPDIR"] = str(runtime_dir)
    return runtime_dir


async def _stop_polling_when_started(dispatcher: Dispatcher) -> None:
    """Stop polling even when the signal arrives during startup."""
    for _ in range(600):
        try:
            await dispatcher.stop_polling()
            return
        except RuntimeError:
            await asyncio.sleep(0.1)
    logger.error("Polling did not start within 60 seconds after a stop signal")


async def process_queue_item(
    channel_key: ChannelKey,
    prompt: str,
    source_messages: list[Message],
    target_session_id: str | None,
    *,
    bot: Bot,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
) -> None:
    """Send a queued prompt to CC; on session change, notify the user."""
    old_session_id = session_manager.get_current_session_id(channel_key)

    # After kill/reset, ignore reply-to-resume on the next message.
    if session_manager.consume_fresh_start(channel_key):
        target_session_id = None

    if target_session_id is not None:
        await session_manager.override_session(channel_key, target_session_id)

    session_changed = target_session_id is not None and target_session_id != old_session_id
    if session_changed and target_session_id:
        chat_id, thread_id = channel_key
        notification = t("ui.session_switched", sid=target_session_id[:8])
        try:
            await bot.send_message(
                chat_id,
                notification,
                reply_markup=topic_keyboard(),
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning(
                "Failed to send session switch notification (stale thread_id=%s)",
                thread_id,
                exc_info=True,
            )

    reply_message = source_messages[-1] if source_messages else None
    if reply_message is None:
        return
    await send_streaming_response(
        reply_message, session_manager, channel_key, prompt, tmux_manager=tmux_manager
    )


async def _start() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await setup_bot_commands(bot)
    except Exception:
        logger.warning("Failed to set Telegram bot commands", exc_info=True)

    topic_config_path = settings.resolve_workspace_path(settings.topic_config_path)
    workspace_root = settings.workspace_root_path
    tmux_sessions_dir = settings.resolve_workspace_path(settings.tmux_sessions_dir)
    _ensure_dedicated_tmux_tmpdir(workspace_root, tmux_sessions_dir)
    topic_config = TopicConfig(str(topic_config_path), str(workspace_root))
    tmux_manager = TmuxManager(
        sessions_dir=tmux_sessions_dir,
    )
    codex_update_service = CodexUpdateService(
        state_path=tmux_sessions_dir / "codex_update.json",
        timeout_sec=settings.codex_update_timeout_sec,
        cooldown_sec=settings.codex_update_cooldown_sec,
        enabled=settings.codex_auto_update_enabled,
    )
    tmux_manager.wire_codex_update_service(codex_update_service)
    tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)
    session_manager = SessionManager(settings, topic_config=topic_config)
    tmux_manager.restore_all(session_manager)
    picker_store = PickerStore()
    bot_defaults = BotDefaults(
        cwd=settings.resolve_workspace_path(settings.default_cwd),
        mcp_config=Path(session_manager.default_mcp_config_path()),
    )
    transcriber = Transcriber(settings)
    forward_batcher = ForwardBatcher(bot=bot, transcriber=transcriber)

    async def _process_queue_item(
        channel_key: ChannelKey,
        prompt: str,
        source_messages: list[Message],
        target_session_id: str | None,
    ) -> None:
        await process_queue_item(
            channel_key,
            prompt,
            source_messages,
            target_session_id,
            bot=bot,
            session_manager=session_manager,
            tmux_manager=tmux_manager,
        )

    message_queue = MessageQueue(bot, session_manager, _process_queue_item)

    dp = Dispatcher()
    auth = AuthMiddleware(allowed_user_ids=settings.allowed_user_ids)
    dp.message.outer_middleware(auth)
    dp.callback_query.outer_middleware(auth)
    dp.message.filter(F.chat.type.in_({ChatType.PRIVATE, ChatType.SUPERGROUP}))

    # Order: commands -> cancel -> mode -> forward -> voice -> photo -> text
    # Forward BEFORE voice/photo so forwarded media is batched, not handled directly.
    # forum_topic_router runs first so topic_config.json is updated BEFORE
    # any text/forward handler tries to read mode/cwd for the new thread.
    dp.include_router(forum_topic_router)
    dp.include_router(commands_router)
    dp.include_router(tail_router)
    dp.include_router(cancel_router)
    dp.include_router(tail_router)
    dp.include_router(mode_router)
    dp.include_router(forward_router)
    dp.include_router(voice_router)
    dp.include_router(photo_router)
    dp.include_router(text_router)

    dp["session_manager"] = session_manager
    dp["transcriber"] = transcriber
    dp["forward_batcher"] = forward_batcher
    dp["message_queue"] = message_queue
    dp["queue"] = message_queue
    dp["settings"] = settings
    dp["topic_config"] = topic_config
    dp["tmux_manager"] = tmux_manager
    dp["codex_update_service"] = codex_update_service
    dp["picker_store"] = picker_store
    dp["bot_defaults"] = bot_defaults

    ensure_tmp_dir(session_manager.file_cache_dir)
    cleanup_old_tmp_files(session_manager.file_cache_dir)
    session_manager.load_mapping()
    session_manager.start_cleanup()

    periodic_cleanup_interval = 6 * 3600

    async def _periodic_tmp_cleanup() -> None:
        while True:
            await asyncio.sleep(periodic_cleanup_interval)
            try:
                deleted = cleanup_old_tmp_files(session_manager.file_cache_dir)
                logger.info("Periodic tmp cleanup: deleted %d files", deleted)
            except Exception:
                logger.warning("Periodic tmp cleanup failed", exc_info=True)

    cleanup_task = asyncio.create_task(_periodic_tmp_cleanup())

    async def _on_shutdown() -> None:
        logger.info("Shutting down: cleaning up sessions...")
        cleanup_task.cancel()
        await forward_batcher.shutdown()
        await message_queue.shutdown()
        await tmux_manager.stop_transcript_watchdog()
        await tmux_manager.stop_modal_watchdog()
        await session_manager.shutdown()
        session_manager.save_mapping()
        tmux_manager.persist_state()

    dp.shutdown.register(_on_shutdown)

    loop = asyncio.get_running_loop()
    _pending_stop: asyncio.Task[None] | None = None

    def _stop() -> None:
        nonlocal _pending_stop
        if _pending_stop is None:
            _pending_stop = asyncio.create_task(_stop_polling_when_started(dp))

    loop.add_signal_handler(signal.SIGTERM, _stop)
    loop.add_signal_handler(signal.SIGINT, _stop)

    recovery_factory = make_recovery_on_event(
        bot,
        session_manager,
        tmux_manager,
        topic_config,
    )
    await tmux_manager.resume_tails(recovery_factory)
    tmux_manager.start_modal_watchdog()
    tmux_manager.start_transcript_watchdog()

    logger.info("Starting bot, allowed users: %d", len(settings.allowed_user_ids))
    await dp.start_polling(bot, handle_signals=False)
    if _pending_stop is not None:
        await _pending_stop


def main() -> None:
    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
