"""Entry point for the public Telegram-Claude-Code bot."""

import asyncio
import logging
import signal
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
from telegram_bot.core.handlers.streaming import send_streaming_response
from telegram_bot.core.handlers.text import router as text_router
from telegram_bot.core.handlers.voice import router as voice_router
from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


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

    topic_config = TopicConfig(settings.topic_config_path, settings.project_root)
    tmux_manager = TmuxManager(
        sessions_dir=Path(settings.project_root) / settings.tmux_sessions_dir,
    )
    tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)
    tmux_manager.restore_all()
    session_manager = SessionManager(settings, topic_config=topic_config)
    transcriber = Transcriber(settings)
    forward_batcher = ForwardBatcher(bot=bot)

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
    dp.include_router(cancel_router)
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
        await session_manager.shutdown()
        session_manager.save_mapping()
        tmux_manager._save_state()

    dp.shutdown.register(_on_shutdown)

    loop = asyncio.get_running_loop()
    _pending_stop: asyncio.Future[None] | None = None

    def _stop() -> None:
        nonlocal _pending_stop
        _pending_stop = asyncio.ensure_future(dp.stop_polling())

    loop.add_signal_handler(signal.SIGTERM, _stop)
    loop.add_signal_handler(signal.SIGINT, _stop)

    logger.info("Starting bot, allowed users: %d", len(settings.allowed_user_ids))
    await dp.start_polling(bot, handle_signals=False)


def main() -> None:
    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
