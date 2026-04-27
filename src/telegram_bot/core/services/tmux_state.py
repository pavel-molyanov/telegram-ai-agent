"""Persistent state for TmuxManager — dataclass, JSON store, orphan scan.

Extracted from `tmux_manager.py` to keep the manager facade small.
Public symbols are re-exported from `tmux_manager` so callers do not need
to know the internal split.

`StateStore.save()` is guarded by `threading.Lock` so concurrent writers
cannot race on the `tmp → os.replace()` sequence. Without the lock, two
simultaneous `_save_state()` calls could both write the same tmp path
and one of the renames could fail or leave a partial file behind
(observed intermittently under high tail activity).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from telegram_bot.core.services.claude import Mode
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


@dataclass
class TmuxSessionState:
    session_name: str
    session_dir: str
    session_id: str | None
    mode: Mode
    cwd: str
    mcp_config: str
    chat_id: int
    offset: int = 0  # bytes already sent to Telegram
    # Legacy state.json entries get "stream-json-legacy" via _normalize_state_dict.
    runner_version: str = "tui-v1"
    provider: str = "claude"
    model: str | None = None
    transcript_path: str | None = None
    base_mcp_config: str | None = None


def _normalize_state_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in runner_version for legacy state.json entries.

    Old stream-json runner wrote entries without runner_version. We can't rely
    on the dataclass default because that would silently label legacy sessions
    as "tui-v1" and mislead downstream migration logic. Returns a new dict so
    the caller's input is not mutated.
    """
    normalized = dict(data)
    if "runner_version" not in normalized:
        normalized["runner_version"] = "stream-json-legacy"
    if "provider" not in normalized:
        normalized["provider"] = "claude"
    if normalized.get("runner_version") == "tui-v1":
        normalized["runner_version"] = "claude-tui-v1"
    normalized.setdefault("model", None)
    normalized.setdefault("transcript_path", None)
    normalized.setdefault("base_mcp_config", normalized.get("mcp_config"))
    return normalized


class StateStore:
    """JSON-backed store for the sessions map. Thread-safe around writes.

    The lock is `threading.Lock` (not asyncio) because the tail loop calls
    `save` from multiple task contexts (send_stream, _run_recovery_tail,
    clear_context) and we don't control which event-loop iteration commits
    the write. A threading lock serialises the critical section across
    tasks without requiring callers to hold an asyncio lock.
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._write_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._state_path

    def exists(self) -> bool:
        return self._state_path.exists()

    def load_raw(self) -> dict[str, Any]:
        """Return parsed state.json as dict, or {} on any read/parse error."""
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text())
        except Exception:
            logger.warning("Failed to load tmux state", exc_info=True)
            return {}
        return raw if isinstance(raw, dict) else {}

    def save(self, sessions: dict[Any, TmuxSessionState]) -> None:
        """Atomic write: tmp + os.replace() so a crash between write and rename
        leaves the previous valid state.json intact. A direct write_text
        truncates the target file first — a crash then yields a partial
        JSON that breaks restore_all on the next bot startup.

        `sessions` is typed loosely because ChannelKey is a tuple and
        cannot be a dict key in JSON — we serialise "chat_id:thread_id".
        """
        data: dict[str, Any] = {}
        for channel_key, state in sessions.items():
            key_str = f"{channel_key[0]}:{channel_key[1]}"
            data[key_str] = asdict(state)

        tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with self._write_lock:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp_path.write_text(json.dumps(data, indent=2))
                os.replace(tmp_path, self._state_path)
            except Exception:
                logger.warning("Failed to save tmux state", exc_info=True)
                with contextlib.suppress(OSError):
                    tmp_path.unlink()


def peek_saved_session(store: StateStore, channel_key: ChannelKey, cwd: str) -> str | None:
    """Return a safely-resumable session_id from state.json, or None.

    All guards must pass:

      1. An entry exists in state.json for this channel_key.
      2. `runner_version == "tui-v1"` — legacy stream-json entries cannot
         be resumed via `--resume` (different transcript format).
      3. `session_id` is non-empty.
      4. `state.cwd == cwd` — topic-cwd change invalidates the resume
         because the saved session points at a different codebase.
      5. The transcript jsonl is present on disk — otherwise CC would
         exit with "Error: Session <uuid> not found" under `--resume`.

    `transcript_path` is resolved lazily through `tmux_manager` so tests
    that patch `telegram_bot.core.services.tmux_manager.transcript_path`
    take effect here too.
    """
    if not store.exists():
        return None
    try:
        raw = json.loads(store.path.read_text())
    except Exception:
        logger.warning("peek_saved_session: state.json unreadable", exc_info=True)
        return None

    key_str = f"{channel_key[0]}:{channel_key[1]}"
    entry = raw.get(key_str) if isinstance(raw, dict) else None
    if not isinstance(entry, dict):
        return None

    data = _normalize_state_dict(entry)
    rv = data.get("runner_version")
    provider = data.get("provider", "claude")
    if not (
        (provider == "claude" and rv in {"tui-v1", "claude-tui-v1"})
        or (provider == "codex" and rv == "codex-tui-v1")
    ):
        logger.debug(
            "peek_saved_session: %s runner_version=%s unsupported, skipping",
            key_str,
            data.get("runner_version"),
        )
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if data.get("cwd") != cwd:
        logger.debug(
            "peek_saved_session: %s cwd changed (state=%s, current=%s), skipping",
            key_str,
            data.get("cwd"),
            cwd,
        )
        return None
    if provider == "codex":
        raw_path = data.get("transcript_path")
        path = Path(raw_path) if isinstance(raw_path, str) else None
    else:
        # Lazy import via the facade so monkey-patches of
        # `tmux_manager.transcript_path` (test harness idiom) take effect.
        from telegram_bot.core.services import tmux_manager as _tm

        path = _tm.transcript_path(cwd, session_id)  # type: ignore[attr-defined]
    if path is None or not path.exists():
        return None
    return session_id


def scan_orphan_tmux_sessions(state_path: Path) -> list[str]:
    """Return sorted names of cc-* tmux sessions that are not tui-v1 in state.

    Used by bot startup (Wave 4 Task 5) to surface tmux sessions left over
    from the legacy stream-json runner. "Orphan" = tmux session named cc-*
    that either has no entry in state.json or whose entry lacks
    runner_version="tui-v1".

    Graceful failure modes (all return []):
    - state_path missing or unreadable JSON
    - tmux binary unavailable (FileNotFoundError)
    - tmux ls returns non-zero (no server / no sessions)

    Does not touch tmux or files beyond reading — safe to call before the
    TmuxManager is instantiated.
    """
    state_markers: dict[str, str] = {}
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text())
        except Exception:
            logger.debug("scan_orphan_tmux_sessions: state.json unreadable", exc_info=True)
            raw = {}
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                name = entry.get("session_name")
                if not isinstance(name, str):
                    continue
                state_markers[name] = entry.get("runner_version", "stream-json-legacy")

    try:
        result = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.debug("scan_orphan_tmux_sessions: tmux binary not found")
        return []
    except Exception:
        logger.debug("scan_orphan_tmux_sessions: tmux ls failed", exc_info=True)
        return []

    if result.returncode != 0:
        logger.debug("scan_orphan_tmux_sessions: tmux ls returncode=%d", result.returncode)
        return []

    orphans: list[str] = []
    for raw_name in result.stdout.splitlines():
        name = raw_name.strip()
        if not name or not name.startswith("cc-"):
            continue
        marker = state_markers.get(name)
        if marker not in {"tui-v1", "claude-tui-v1", "codex-tui-v1"}:
            orphans.append(name)

    return sorted(orphans)
