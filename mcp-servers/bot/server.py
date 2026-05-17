#!/usr/bin/env python3
"""Bot MCP server — sends files and messages to Telegram chats.

Runs as a stdio MCP server, started by Claude Code via .mcp.bot.json.
BOT_TOKEN must be set in environment (read from .env by start.sh).
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

mcp = FastMCP("bot")

_TELEGRAM_API = "https://api.telegram.org"
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — Telegram Bot API limit
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB — Telegram photo limit
_MAX_MESSAGE_LEN = 4096
_TELEGRAM_CAPTION_LEN = 1024
_MAX_MEDIA_TEXT_LEN = 4000
_MAX_MESSAGE_CHUNKS = 10
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_GALLERY_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MIN_GALLERY_ITEMS = 2
_MAX_GALLERY_ITEMS = 10
_PHOTO_FALLBACK_ERRORS = (
    "IMAGE_PROCESS_FAILED",
    "PHOTO_INVALID_DIMENSIONS",
    "wrong file identifier/http url specified",
    "failed to get http url content",
)
_VALID_PARSE_MODES = {"html": "HTML", "markdownv2": "MarkdownV2"}


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


def _normalize_parse_mode(value: str | None) -> tuple[str | None, str | None]:
    """Validate and canonicalize the parse_mode argument.

    Returns (canonical_value, error). Empty input → (None, None) — no formatting.
    Legacy "Markdown" v1 is intentionally rejected; Telegram treats it as deprecated.
    """
    if value is None:
        return None, None
    canonical = _VALID_PARSE_MODES.get(value.lower())
    if canonical is None:
        return None, (f"Ошибка: parse_mode должен быть HTML или MarkdownV2, получено {value!r}")
    return canonical, None


def _is_photo_marker_error(description: str) -> bool:
    """True if the error description matches one of Telegram's photo-specific markers.

    Used to skip parse_mode-fallback for photo errors so the outer send_image
    photo-fallback (sendPhoto → sendDocument) sees the original error.
    """
    lowered = description.lower()
    return any(marker.lower() in lowered for marker in _PHOTO_FALLBACK_ERRORS)


def _extract_error(resp: httpx.Response) -> str:
    try:
        raw = str(resp.json().get("description", resp.text[:200]))
    except Exception:
        raw = resp.text[:200]
    # Sanitize control chars — Telegram error descriptions are echoed into
    # MCP return strings and logger.warning; bare \n/\r could split log lines
    # or break the [parse_mode_dropped: ...] prefix marker.
    return raw.replace("\n", "\\n").replace("\r", "\\r")


def _message_chunks(text: str) -> list[str] | None:
    if not text:
        return [""]
    chunks = [text[i : i + _MAX_MESSAGE_LEN] for i in range(0, len(text), _MAX_MESSAGE_LEN)]
    if len(chunks) > _MAX_MESSAGE_CHUNKS:
        return None
    return chunks


def _post_message(
    token: str,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    if not text:
        return "Ошибка: сообщение пустое"
    chunks = _message_chunks(text)
    if chunks is None:
        return (
            f"Ошибка: сообщение слишком длинное "
            f"(макс {_MAX_MESSAGE_LEN * _MAX_MESSAGE_CHUNKS} символов)"
        )
    # Pre-flight: parse_mode + multi-chunk would split mid-tag and corrupt formatting
    # in unpredictable ways. Refuse before any HTTP call so the caller can split itself.
    if parse_mode is not None and len(chunks) > 1:
        return (
            f"Ошибка: parse_mode не поддерживается для текста длиннее {_MAX_MESSAGE_LEN} "
            f"символов (split может порвать теги)"
        )
    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    success_label = (
        "Отправлено сообщение" if len(chunks) == 1 else f"Отправлено сообщений: {len(chunks)}"
    )
    try:
        with httpx.Client(timeout=60) as client:
            for chunk in chunks:
                data: dict[str, str] = {"chat_id": str(chat_id), "text": chunk}
                if thread_id is not None:
                    data["message_thread_id"] = str(thread_id)
                if parse_mode is not None:
                    data["parse_mode"] = parse_mode
                resp = client.post(url, data=data)
                if resp.status_code == 200:
                    continue
                error = _extract_error(resp)
                # Only retry without parse_mode if the error wasn't a known photo marker
                # (those propagate up so send_image's photo-fallback can act).
                if parse_mode is not None and not _is_photo_marker_error(error):
                    retry_data = {k: v for k, v in data.items() if k != "parse_mode"}
                    retry_resp = client.post(url, data=retry_data)
                    if retry_resp.status_code == 200:
                        logger.warning(
                            "parse_mode dropped (parse_mode=%s, error=%s)",
                            parse_mode,
                            error,
                        )
                        return f"[parse_mode_dropped: {error}] {success_label}"
                    return f"Ошибка {retry_resp.status_code}: {_extract_error(retry_resp)}"
                return f"Ошибка {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Ошибка: timeout при отправке"
    except Exception as exc:
        return f"Ошибка: {exc}"
    return success_label


def _split_media_caption(caption: str) -> tuple[str, str, str | None]:
    if len(caption) > _MAX_MEDIA_TEXT_LEN:
        return (
            "",
            "",
            f"Ошибка: caption слишком длинный (макс {_MAX_MEDIA_TEXT_LEN} символов)",
        )
    return caption[:_TELEGRAM_CAPTION_LEN], caption[_TELEGRAM_CAPTION_LEN:], None


def _send_to_telegram(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
    *,
    method: str = "sendDocument",
    file_field: str = "document",
    success_label: str = "Отправлен",
) -> str:
    """Send file via Telegram Bot API."""
    media_caption, caption_tail, caption_error = _split_media_caption(caption)
    if caption_error:
        return caption_error
    # Pre-flight size check — skips opening httpx.Client for oversized files
    # and ensures retry path doesn't waste a second 50 MB upload to Telegram.
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return f"Ошибка: {exc}"
    if size > _MAX_FILE_SIZE:
        return f"Ошибка: файл слишком большой ({size // 1024 // 1024} МБ, макс 50 МБ)"
    url = f"{_TELEGRAM_API}/bot{token}/{method}"
    data: dict[str, str] = {"chat_id": str(chat_id), "caption": media_caption}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    if parse_mode is not None:
        data["parse_mode"] = parse_mode
    success_full = f"{success_label}: {file_path.name}"
    try:
        with httpx.Client(timeout=60) as client, open(file_path, "rb") as f:
            resp = client.post(url, data=data, files={file_field: (file_path.name, f)})
        if resp.status_code == 200:
            result = success_full
            if caption_tail:
                tail_result = _post_message(token, chat_id, caption_tail, thread_id, parse_mode)
                result = f"{result}; {tail_result}"
            return result
        error = _extract_error(resp)
        if parse_mode is not None and not _is_photo_marker_error(error):
            retry_data = {k: v for k, v in data.items() if k != "parse_mode"}
            with httpx.Client(timeout=60) as client, open(file_path, "rb") as f:
                retry_resp = client.post(
                    url, data=retry_data, files={file_field: (file_path.name, f)}
                )
            if retry_resp.status_code == 200:
                logger.warning(
                    "parse_mode dropped (parse_mode=%s, error=%s)",
                    parse_mode,
                    error,
                )
                result = f"[parse_mode_dropped: {error}] {success_full}"
                if caption_tail:
                    tail_result = _post_message(token, chat_id, caption_tail, thread_id, parse_mode)
                    result = f"{result}; {tail_result}"
                return result
            return f"Ошибка {retry_resp.status_code}: {_extract_error(retry_resp)}"
        return f"Ошибка {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Ошибка: timeout при отправке"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _send_document(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send file as document via Telegram Bot API."""
    return _send_to_telegram(token, chat_id, file_path, caption, thread_id, parse_mode)


def _send_photo(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send image as photo via Telegram Bot API (renders inline, not as file)."""
    return _send_to_telegram(
        token,
        chat_id,
        file_path,
        caption,
        thread_id,
        parse_mode,
        method="sendPhoto",
        file_field="photo",
        success_label="Отправлено фото",
    )


def _send_image_path(
    token: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    size = file_path.stat().st_size
    is_image = file_path.suffix.lower() in _IMAGE_EXTENSIONS and size <= _MAX_PHOTO_SIZE
    if not is_image:
        return _send_document(token, chat_id, file_path, caption, thread_id, parse_mode)
    result = _send_photo(token, chat_id, file_path, caption, thread_id, parse_mode)
    if _is_photo_fallback_error(result):
        return _send_document(token, chat_id, file_path, caption, thread_id, parse_mode)
    return result


def _gallery_media_payload(
    file_paths: list[Path],
    caption: str,
    parse_mode: str | None = None,
) -> list[dict[str, str]]:
    media: list[dict[str, str]] = []
    for idx, _file_path in enumerate(file_paths):
        item = {"type": "photo", "media": f"attach://photo{idx}"}
        if idx == 0 and caption:
            item["caption"] = caption
            if parse_mode is not None:
                item["parse_mode"] = parse_mode
        media.append(item)
    return media


def _post_media_group(
    token: str,
    chat_id: int,
    file_paths: list[Path],
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> httpx.Response:
    url = f"{_TELEGRAM_API}/bot{token}/sendMediaGroup"
    media = _gallery_media_payload(file_paths, caption, parse_mode)
    data: dict[str, str] = {
        "chat_id": str(chat_id),
        "media": json.dumps(media, ensure_ascii=False),
    }
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)

    with ExitStack() as stack:
        files = {
            f"photo{idx}": (file_path.name, stack.enter_context(open(file_path, "rb")))
            for idx, file_path in enumerate(file_paths)
        }
        with httpx.Client(timeout=60) as client:
            return client.post(url, data=data, files=files)


def _send_image_gallery(
    token: str,
    chat_id: int,
    file_paths: list[Path],
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    media_caption, caption_tail, caption_error = _split_media_caption(caption)
    if caption_error:
        return caption_error

    success_label = f"Отправлена галерея: {len(file_paths)} фото"
    try:
        resp = _post_media_group(token, chat_id, file_paths, media_caption, thread_id, parse_mode)
        if resp.status_code == 200:
            result = success_label
            if caption_tail:
                tail_result = _post_message(token, chat_id, caption_tail, thread_id, parse_mode)
                result = f"{result}; {tail_result}"
            return result

        error = _extract_error(resp)
        if parse_mode is not None and media_caption and not _is_photo_marker_error(error):
            retry_resp = _post_media_group(token, chat_id, file_paths, media_caption, thread_id)
            if retry_resp.status_code == 200:
                logger.warning(
                    "parse_mode dropped (parse_mode=%s, error=%s)",
                    parse_mode,
                    error,
                )
                result = f"[parse_mode_dropped: {error}] {success_label}"
                if caption_tail:
                    tail_result = _post_message(token, chat_id, caption_tail, thread_id, parse_mode)
                    result = f"{result}; {tail_result}"
                return result
            return f"Ошибка {retry_resp.status_code}: {_extract_error(retry_resp)}"
        return f"Ошибка {resp.status_code}: {error}"
    except httpx.TimeoutException:
        return "Ошибка: timeout при отправке"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _send_gallery_sequential(
    token: str,
    chat_id: int,
    file_paths: list[Path],
    caption: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    results = []
    for idx, file_path in enumerate(file_paths):
        item_caption = caption if idx == 0 else ""
        item_parse_mode = parse_mode if idx == 0 and item_caption else None
        result = _send_image_path(
            token, chat_id, file_path, item_caption, thread_id, item_parse_mode
        )
        results.append(result)
        if result.startswith("Ошибка"):
            return (
                "Ошибка: sendMediaGroup не прошёл; "
                f"fallback остановлен на {file_path.name}: {result}"
            )
    return f"sendMediaGroup не прошёл; отправлено по одному: {len(results)} фото"


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


def _resolve_gallery_paths(file_paths: list[str]) -> tuple[list[Path] | None, str | None]:
    if not isinstance(file_paths, list):
        return None, "Ошибка: file_paths должен быть списком путей"
    if not (_MIN_GALLERY_ITEMS <= len(file_paths) <= _MAX_GALLERY_ITEMS):
        return None, "Ошибка: file_paths должен содержать 2-10 изображений"

    resolved_paths = []
    for idx, file_path in enumerate(file_paths):
        resolved_path, path_error = _resolve_file_path(file_path)
        if path_error:
            return None, f"Ошибка: file_paths[{idx}]: {path_error}"
        assert resolved_path is not None

        suffix = resolved_path.suffix.lower()
        if suffix not in _GALLERY_PHOTO_EXTENSIONS:
            return None, f"Ошибка: file_paths[{idx}] не является изображением: {file_path}"
        try:
            size = resolved_path.stat().st_size
        except OSError as exc:
            return None, f"Ошибка: {exc}"
        if size > _MAX_PHOTO_SIZE:
            return None, (
                f"Ошибка: фото слишком большое ({size // 1024 // 1024} МБ, макс 10 МБ): {file_path}"
            )
        resolved_paths.append(resolved_path)
    return resolved_paths, None


@mcp.tool()
def send_message(
    text: str,
    chat_id: int | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send a text message to the current Telegram chat/topic.

    In bot-launched sessions, chat/thread routing is taken from MCP env.

    parse_mode: optional Telegram parse mode ("HTML" or "MarkdownV2"). Default: plain text.
    MarkdownV2 requires escaping _*[]()~`>#+-=|{}.! — prefer HTML for agent-generated text.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    normalized_pm, pm_error = _normalize_parse_mode(parse_mode)
    if pm_error:
        return pm_error
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    assert resolved_chat is not None
    return _post_message(token, resolved_chat, text, resolved_thread, normalized_pm)


@mcp.tool()
def send_document(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send a document/file to the current Telegram chat/topic.

    parse_mode: optional Telegram parse mode ("HTML" or "MarkdownV2"). Default: plain text.
    MarkdownV2 requires escaping _*[]()~`>#+-=|{}.! — prefer HTML for agent-generated text.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    normalized_pm, pm_error = _normalize_parse_mode(parse_mode)
    if pm_error:
        return pm_error
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    return _send_document(
        token, resolved_chat, resolved_path, caption, resolved_thread, normalized_pm
    )


@mcp.tool()
def send_image(
    file_path: str,
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send an image to the current Telegram chat/topic, inline when possible.

    parse_mode: optional Telegram parse mode ("HTML" or "MarkdownV2"). Default: plain text.
    MarkdownV2 requires escaping _*[]()~`>#+-=|{}.! — prefer HTML for agent-generated text.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    normalized_pm, pm_error = _normalize_parse_mode(parse_mode)
    if pm_error:
        return pm_error
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_path, path_error = _resolve_file_path(file_path)
    if path_error:
        return path_error
    assert resolved_chat is not None and resolved_path is not None
    return _send_image_path(
        token, resolved_chat, resolved_path, caption, resolved_thread, normalized_pm
    )


@mcp.tool()
def send_image_gallery(
    file_paths: list[str],
    caption: str = "",
    chat_id: int | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send 2-10 photos as one Telegram gallery/media group.

    Caption is attached to the first photo only. In bot-launched sessions,
    chat/thread routing is taken from MCP env.

    parse_mode: optional Telegram parse mode ("HTML" or "MarkdownV2"). Default: plain text.
    MarkdownV2 requires escaping _*[]()~`>#+-=|{}.! — prefer HTML for agent-generated text.
    """
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return "Ошибка: BOT_TOKEN не настроен"
    normalized_pm, pm_error = _normalize_parse_mode(parse_mode)
    if pm_error:
        return pm_error
    resolved_chat, resolved_thread, error = _resolve_routing(chat_id, thread_id)
    if error:
        return error
    resolved_paths, paths_error = _resolve_gallery_paths(file_paths)
    if paths_error:
        return paths_error
    assert resolved_chat is not None and resolved_paths is not None

    result = _send_image_gallery(
        token, resolved_chat, resolved_paths, caption, resolved_thread, normalized_pm
    )
    if _is_photo_fallback_error(result):
        return _send_gallery_sequential(
            token, resolved_chat, resolved_paths, caption, resolved_thread, normalized_pm
        )
    return result


if __name__ == "__main__":
    mcp.run()
