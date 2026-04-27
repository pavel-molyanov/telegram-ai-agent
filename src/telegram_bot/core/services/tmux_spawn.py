"""Tmux session spawning helpers — async/sync variants and pane utilities.

Extracted from `tmux_manager.py`. All `subprocess.run` invocations in the
async path are wrapped in `asyncio.to_thread` (Wave 3 B4 fix): running
subprocess.run synchronously inside an async function blocks the event
loop during the tmux server handshake (20-80 ms typically; seconds under
load). `asyncio.to_thread` releases the loop while the external process
runs.

`subprocess` is imported at module level so tests can monkey-patch
`telegram_bot.core.services.tmux_spawn.subprocess.run`. Callers in the
manager facade keep their own `subprocess` import for patches that target
`tmux_manager.subprocess.run` directly (legacy test API).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

# Shared clock for await_prompt_ready + transcript poll (Wave 2 Decision 7).
SPAWN_READINESS_BUDGET_SEC = 30.0

# Transient stderr fragments from `tmux new-session` that indicate the server
# is mid-shutdown (exit-empty on, default) after `kill-session` removed the
# last session. One retry after a short pause lets the server fully exit
# and re-spawn cleanly. Regression observed 2026-04-21 on "Новый чат".
TMUX_NEW_SESSION_TRANSIENT_ERRORS = (
    "server exited unexpectedly",
    "no server running",
    "error connecting",
)
TMUX_NEW_SESSION_RETRY_DELAY_SEC = 0.2

# Modal watchdog cadence. Every MODAL_WATCHDOG_INTERVAL_SEC the watchdog
# walks all active sessions, captures their pane, and posts an idle-modal
# alert for any that advertise a dismiss token in the footer. 8 s is the
# sweet spot between "user notices within a handful of seconds" and "we're
# not spamming capture-pane for no reason".
MODAL_WATCHDOG_INTERVAL_SEC = 8.0


def make_session_name(channel_key: ChannelKey, *, prefix: str = "cc-") -> str:
    """Deterministic tmux session name per channel.

    Negative chat_ids (supergroups) are prefixed with `n` so the pane id
    does not collide with a private chat of the same absolute value.
    Previously `abs(chat_id)` flattened the sign, which in theory could
    conflict between a user (chat_id=42) and a supergroup (chat_id=-42).
    """
    chat_id, thread_id = channel_key
    suffix = str(thread_id) if thread_id is not None else "0"
    chat_part = f"n{-chat_id}" if chat_id < 0 else str(chat_id)
    return f"{prefix}{chat_part}-{suffix}"


def tmux_alive(session_name: str) -> bool:
    """Synchronous tmux has-session check.

    Kept synchronous because it is called from bot-startup (restore_all)
    and from non-async paths. Async callers should use `asyncio.to_thread`.
    """
    result = subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True)
    return result.returncode == 0


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


async def query_pane_width(session_name: str) -> int | None:
    """Return `tmux display-message -p '#{pane_width}'` as int, or None on
    any subprocess failure. Invoked only when the modal-diag logger is
    enabled, so the overhead is off the hot path in prod."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "display-message", "-p", "-t", session_name, "#{pane_width}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        logger.debug(
            "tmux display-message pane_width rc=%d stderr=%r",
            result.returncode,
            (result.stderr or "")[:200],
        )
        return None
    raw = (result.stdout or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def spawn_tmux_sync(
    *,
    name: str,
    session_dir: Path,
    cwd: str,
    startup_cmd: list[str],
) -> bool:
    """Synchronous respawn used by `restore_all` on bot startup.

    Skips `await_prompt_ready` because `restore_all` runs from a sync
    context on bot startup. Readiness is re-checked lazily on the next
    user message via `ensure_exec_mode_ready`. Returns False on tmux
    failure — caller logs at WARNING.
    """
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        result = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                "200",
                "-y",
                "50",
                *startup_cmd,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except Exception:
        logger.warning("tmux spawn raised for %s", name, exc_info=True)
        return False
    return result.returncode == 0
