#!/usr/bin/env python3
"""Bot MCP server — sends files and messages to Telegram chats.

Runs as a stdio MCP server, started by Claude Code via .mcp.bot.json.
BOT_TOKEN must be set in environment (read from .env by start.sh).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)

mcp = FastMCP("bot")

_TELEGRAM_API = "https://api.telegram.org"
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — Telegram Bot API limit
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB — Telegram photo limit
_MAX_MESSAGE_LEN = 4096
_MAX_CAPTION_LEN = 1024
_MAX_MESSAGE_CHUNKS = 10
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_PHOTO_FALLBACK_ERRORS = (
    "IMAGE_PROCESS_FAILED",
    "PHOTO_INVALID_DIMENSIONS",
    "wrong file identifier/http url specified",
    "failed to get http url content",
)


def _env_int(name: str) -> tuple[int | None, str | None]:
    raw = os.environ.get(name, "")
    if raw == "":
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, f"Ошибка: {name} должен быть int, получено {raw!r}"


def _resolve_routing(
    chat_id: int | None = None, thread_id: int | None = None
) -> tuple[int | None, int | None, str | None]:
    env_chat_id, chat_error = _env_int("TELEGRAM_CHAT_ID")
    if chat_error:
        return None, None, chat_error
    env_thread_id, thread_error = _env_int("TELEGRAM_THREAD_ID")
    if thread_error:
        return None, None, thread_error

    lock_context = os.environ.get("TELEGRAM_CONTEXT_LOCK") == "1"
    resolved_chat = chat_id if chat_id is not None else env_chat_id
    resolved_thread = thread_id if thread_id is not None else env_thread_id

    if resolved_chat is None:
        return None, None, "Ошибка: chat_id не передан и TELEGRAM_CHAT_ID не настроен"
    if lock_context:
        if env_chat_id is None:
            return None, None, "Ошибка: TELEGRAM_CONTEXT_LOCK=1, но TELEGRAM_CHAT_ID не настроен"
        if chat_id is not None and chat_id != env_chat_id:
            return None, None, "Ошибка: chat_id не совпадает с текущей Telegram-сессией"  # noqa: RUF001
        if thread_id is not None and thread_id != env_thread_id:
            return None, None, "Ошибка: thread_id не совпадает с текущей Telegram-сессией"  # noqa: RUF001
    return resolved_chat, resolved_thread, None


def _message_chunks(text: str) -> list[str] | None:
    if not text:
        return [""]
    chunks = [text[i : i + _MAX_MESSAGE_LEN] for i in range(0, len(text), _MAX_MESSAGE_LEN)]
    if len(chunks) > _MAX_MESSAGE_CHUNKS:
        return None
    return chunks


def _post_message(token: str, chat_id: int, text: str, thread_id: int | None = None) -> str:
    if not text:
        return "Ошибка: сообщение пустое"
    chunks = _message_chunks(text)
    if chunks is None:
        return (
            f"Ошибка: сообщение слишком длинное "
            f"(макс {_MAX_MESSAGE_LEN * _MAX_MESSAGE_CHUNKS} символов)"
        )
    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=60) as client:
            for chunk in chunks:
                data: dict[str, str] = {"chat_id": str(chat_id), "text": chunk}
                if thread_id is not None:
                    data["message_thread_id"] = str(thread_id)
                resp = client.post(url, data=data)
                if resp.status_code != 200:
                    try:
                        error = resp.json().get("description", resp.text[:200])
                    except Exception:
                        error = resp.text[:200]
                    return f"Ошибка {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Ошибка: timeout при отправке"
    except Exception as exc:
        return f"Ошибка: {exc}"
    return "Отправлено сообщение" if len(chunks) == 1 else f"Отправлено сообщений: {len(chunks)}"


def _send_to_telegram(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    *,
    method: str = "sendDocument",
    file_field: str = "document",
    success_label: str = "Отправлен",
) -> str:
    """Send file via Telegram Bot API."""
    url = f"{_TELEGRAM_API}/bot{token}/{method}"
    media_caption = caption[:_MAX_CAPTION_LEN]
    caption_tail = caption[_MAX_CAPTION_LEN:]
    data: dict[str, str] = {"chat_id": str(chat_id), "caption": media_caption}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    try:
        with httpx.Client(timeout=60) as client, open(file_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > _MAX_FILE_SIZE:
                return f"Ошибка: файл слишком большой ({size // 1024 // 1024} МБ, макс 50 МБ)"
            resp = client.post(url, data=data, files={file_field: (file_path.name, f)})
        if resp.status_code == 200:
            result = f"{success_label}: {file_path.name}"
            if caption_tail:
                tail_result = _post_message(token, chat_id, caption_tail, thread_id)
                result = f"{result}; {tail_result}"
            return result
        try:
            error = resp.json().get("description", resp.text[:200])
        except Exception:
            error = resp.text[:200]
        return f"Ошибка {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Ошибка: timeout при отправке"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _send_document(
    token: str, chat_id: int, file_path: Path, caption: str, thread_id: int | None = None
) -> str:
    """Send file as document via Telegram Bot API."""
    return _send_to_telegram(token, chat_id, file_path, caption, thread_id)


def _send_photo(
    token: str, chat_id: int, file_path: Path, caption: str, thread_id: int | None = None
) -> str:
    """Send image as photo via Telegram Bot API (renders inline, not as file)."""
    return _send_to_telegram(
        token,
        chat_id,
        file_path,
        caption,
        thread_id,
        method="sendPhoto",
        file_field="photo",
        success_label="Отправлено фото",
    )


def _is_photo_fallback_error(result: str) -> bool:
    if not result.startswith("Ошибка 400"):
        return False
    lowered = result.lower()
    return any(marker.lower() in lowered for marker in _PHOTO_FALLBACK_ERRORS)


def _resolve_file_path(file_path: str) -> tuple[Path | None, str | None]:
    if not file_path:
        return None, "Ошибка: file_path не передан"
    resolved = Path(file_path)
    if not resolved.exists():
        return None, f"Ошибка: файл не найден: {file_path}"
    if not resolved.is_file():
        return None, f"Ошибка: не является файлом: {file_path}"
    return resolved, None


@mcp.tool()
def send_message(
    text: str,
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send a text message to the current Telegram chat/topic.

    In bot-launched sessions, chat/thread routing is taken from MCP env.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    assert resolved_chat is not None
    return _post_message(token, resolved_chat, text, resolved_thread)


@mcp.tool()
def send_document(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send a document/file to the current Telegram chat/topic."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)


@mcp.tool()
def send_image(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> str:
    """Send an image to the current Telegram chat/topic, inline when possible."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    size = resolved_path.stat().st_size
    is_image = resolved_path.suffix.lower() in _IMAGE_EXTENSIONS and size <= _MAX_PHOTO_SIZE
    if not is_image:
        return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)
    result = _send_photo(token, resolved_chat, resolved_path, caption, resolved_thread)
    if _is_photo_fallback_error(result):
        return _send_document(token, resolved_chat, resolved_path, caption, resolved_thread)
    return result


if __name__ == "__main__":
    mcp.run()
