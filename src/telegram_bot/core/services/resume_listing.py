"""List resumable Claude Code and Codex TUI sessions for a cwd."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from telegram_bot.core.tui.paths import _CODEX_SESSION_ID_RE, _SESSION_ID_RE, cwd_to_slug

EngineName = Literal["claude", "codex"]
_SOFT_CAP_BYTES = 64 * 1024
_PREVIEW_LIMIT = 60


@dataclass(frozen=True)
class SessionEntry:
    provider: EngineName
    session_id: str
    transcript_path: Path
    preview: str
    mtime: float
    size_bytes: int


def list_sessions(cwd: str | Path, *, home: Path | None = None) -> list[SessionEntry]:
    """Return Claude + Codex TUI sessions scoped to cwd, newest first."""
    home = home or Path.home()
    cwd_path = Path(cwd)
    entries = [
        *_list_claude_sessions(cwd_path, home),
        *_list_codex_sessions(cwd_path, home),
    ]
    return sorted(entries, key=lambda entry: entry.mtime, reverse=True)


def _list_claude_sessions(cwd: Path, home: Path) -> list[SessionEntry]:
    root = home / ".claude" / "projects" / cwd_to_slug(cwd)
    if not root.exists():
        return []
    entries: list[SessionEntry] = []
    for path in root.glob("*.jsonl"):
        session_id = path.stem
        if not _SESSION_ID_RE.fullmatch(session_id):
            continue
        stat = _safe_stat(path)
        if stat is None:
            continue
        entries.append(
            SessionEntry(
                provider="claude",
                session_id=session_id,
                transcript_path=path,
                preview=_preview_claude(path) or session_id[:8],
                mtime=stat.st_mtime,
                size_bytes=stat.st_size,
            )
        )
    return entries


def _list_codex_sessions(cwd: Path, home: Path) -> list[SessionEntry]:
    root = home / ".codex" / "sessions"
    if not root.exists():
        return []
    entries: list[SessionEntry] = []
    for path in root.glob("**/*.jsonl"):
        meta = _codex_meta(path)
        if meta is None:
            continue
        session_id, session_cwd = meta
        if not _same_cwd(session_cwd, cwd) or not _CODEX_SESSION_ID_RE.fullmatch(session_id):
            continue
        stat = _safe_stat(path)
        if stat is None:
            continue
        entries.append(
            SessionEntry(
                provider="codex",
                session_id=session_id,
                transcript_path=path,
                preview=_preview_codex(path) or session_id[:8],
                mtime=stat.st_mtime,
                size_bytes=stat.st_size,
            )
        )
    return entries


def _safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _iter_jsonl_soft(path: Path) -> Iterator[object]:
    read = 0
    try:
        with path.open("rb") as f:
            for raw in f:
                read += len(raw)
                if read > _SOFT_CAP_BYTES:
                    return
                try:
                    yield json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _iter_jsonl_tail(path: Path, *, cap_bytes: int = 256 * 1024) -> Iterator[object]:
    """Yield parsed JSONL rows newest-first from the tail of a transcript."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > cap_bytes:
                f.seek(size - cap_bytes)
                f.readline()  # Drop a possibly partial first line.
            raw = f.read()
    except OSError:
        return

    for line in reversed(raw.splitlines()):
        try:
            yield json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue


def get_last_assistant_message(provider: EngineName, transcript_path: Path) -> str | None:
    """Return the last user-facing assistant answer from a TUI transcript."""
    if provider == "claude":
        return _last_claude_assistant_message(transcript_path)
    if provider == "codex":
        return _last_codex_assistant_message(transcript_path)
    return None


def _last_claude_assistant_message(path: Path) -> str | None:
    for data in _iter_jsonl_tail(path):
        if not isinstance(data, dict) or data.get("type") != "assistant":
            continue
        text = _extract_text(data.get("message")).strip()
        if text:
            return text
    return None


def _last_codex_assistant_message(path: Path) -> str | None:
    fallback: str | None = None
    for data in _iter_jsonl_tail(path):
        if not isinstance(data, dict) or data.get("type") != "event_msg":
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "agent_message":
            continue
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        text = message.strip()
        if payload.get("phase") == "final_answer":
            return text
        if fallback is None:
            fallback = text
    return fallback


def _preview_claude(path: Path) -> str:
    for data in _iter_jsonl_soft(path):
        if not isinstance(data, dict) or data.get("type") != "user":
            continue
        text = _extract_text(data.get("message"))
        if _meaningful_preview(text):
            return _truncate(text)
    return ""


def _preview_codex(path: Path) -> str:
    for data in _iter_jsonl_soft(path):
        if not isinstance(data, dict) or data.get("type") != "event_msg":
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "user_message":
            continue
        text = _extract_text(payload.get("message") or payload.get("text"))
        if _meaningful_preview(text):
            return _truncate(text)
    return ""


def _codex_meta(path: Path, *, max_records: int = 3) -> tuple[str, str] | None:
    for idx, data in enumerate(_iter_jsonl_soft(path)):
        if idx >= max_records:
            return None
        if not isinstance(data, dict) or data.get("type") != "session_meta":
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("originator") != "codex-tui":
            continue
        source = payload.get("source")
        if isinstance(source, dict) and "subagent" in source:
            continue
        if source == "subagent":
            continue
        session_id = payload.get("id")
        cwd = payload.get("cwd")
        if isinstance(session_id, str) and isinstance(cwd, str):
            return session_id, cwd
    return None


def _extract_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _meaningful_preview(text: str) -> bool:
    stripped = text.strip()
    return bool(
        stripped
        and not stripped.startswith("<command-")
        and not stripped.startswith("<system-reminder>")
        and "hook-attachment" not in stripped[:200]
    )


def _truncate(text: str) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= _PREVIEW_LIMIT:
        return normalized
    return normalized[: _PREVIEW_LIMIT - 1].rstrip() + "…"


def _normalize_cwd(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _same_cwd(left: str | Path, right: str | Path) -> bool:
    return _normalize_cwd(left) == _normalize_cwd(right)
