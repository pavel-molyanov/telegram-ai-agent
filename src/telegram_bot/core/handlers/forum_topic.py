"""Forum-topic lifecycle handler — auto-registers new topics in topic_config.json.

Telegram fires `forum_topic_created` and `forum_topic_edited` updates whenever
a topic appears or is renamed (regardless of who did it — bot via API, user
via Telegram UI, or another admin). We catch both and keep topic_config.json
in sync, so a freshly created topic immediately works with the bot's default
mode/cwd instead of being an empty room until someone manually edits config.

Topic deletion is intentionally NOT auto-removed from config — losing the
config entry on accidental deletion would silently strip cwd/mcp settings
the user spent time configuring. Manual cleanup is safer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import DEFAULT_MODE

logger = logging.getLogger(__name__)

router = Router(name="forum_topic")

# aiogram dispatches updates concurrently. Without this lock, two near-simultaneous
# topic events could each read the same on-disk snapshot and overwrite the other's
# write — silently dropping one of the two new topics from the config.
_config_lock = asyncio.Lock()


def _resolve_config_path(settings: Settings) -> Path:
    """Return absolute path to topic_config.json, resolving relative paths against project_root."""
    p = Path(settings.topic_config_path)
    return p if p.is_absolute() else Path(settings.project_root) / p


def _new_entry(name: str) -> dict[str, Any]:
    """Default topic entry — for fresh registration and edited-but-unknown race recovery."""
    return {
        "name": name,
        "type": "project",
        "mode": DEFAULT_MODE,
        "cwd": None,
        "mcp_config": None,
    }


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"topics": {}}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("topic_config.json is malformed; auto-register skipped")
        raise
    if not isinstance(data, dict):
        logger.warning("topic_config.json top-level is not an object; auto-register skipped")
        raise json.JSONDecodeError("top-level not an object", "", 0)
    data.setdefault("topics", {})
    return data


def _save_config(path: Path, data: dict[str, Any]) -> None:
    """Atomic write via temp file + os.replace — readers see either the old or new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@router.message(F.forum_topic_created)
async def on_topic_created(message: Message, settings: Settings, bot: Bot) -> None:
    """Add a new topic to topic_config.json with default mode/cwd, then post a welcome."""
    if message.forum_topic_created is None or message.message_thread_id is None:
        return
    name = message.forum_topic_created.name
    thread_id = message.message_thread_id
    chat_id = message.chat.id
    config_path = _resolve_config_path(settings)

    newly_registered = False
    async with _config_lock:
        try:
            config = _load_config(config_path)
        except json.JSONDecodeError:
            return
        key = str(thread_id)
        if key in config["topics"]:
            # Already registered (e.g. CC pre-registered or restart re-fired event).
            return
        config["topics"][key] = _new_entry(name)
        _save_config(config_path, config)
        newly_registered = True

    logger.info("Auto-registered topic thread_id=%d name=%r", thread_id, name)

    if not newly_registered:
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=t("ui.topic_welcome"),
            parse_mode=ParseMode.HTML,
            disable_notification=True,
            reply_markup=topic_keyboard(),
        )
    except TelegramBadRequest:
        logger.warning(
            "Failed to send welcome to thread_id=%d (deleted or perms missing)",
            thread_id,
            exc_info=True,
        )


@router.message(F.forum_topic_edited)
async def on_topic_edited(message: Message, settings: Settings) -> None:
    """Sync the renamed name into topic_config.json (other fields untouched)."""
    if message.forum_topic_edited is None or message.message_thread_id is None:
        return
    new_name = message.forum_topic_edited.name
    if new_name is None:
        # icon-only edit — name didn't change
        return
    thread_id = message.message_thread_id
    config_path = _resolve_config_path(settings)

    async with _config_lock:
        try:
            config = _load_config(config_path)
        except json.JSONDecodeError:
            return
        key = str(thread_id)
        entry = config["topics"].get(key)
        if entry is None:
            # Rename event arrived before our 'created' handler ran — register on the fly.
            config["topics"][key] = _new_entry(new_name)
        else:
            entry["name"] = new_name
        _save_config(config_path, config)

    logger.info("Topic thread_id=%d renamed to %r", thread_id, new_name)
