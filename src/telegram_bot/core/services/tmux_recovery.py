"""Bot-startup tmux recovery — restore persisted sessions and tail transcripts.

Extracted from `tmux_manager.py`. Functions take the manager explicitly
rather than importing it, to keep this module free of circular imports
and usable in tests with a duck-typed manager.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config
from telegram_bot.core.services.claude import StreamEvent
from telegram_bot.core.services.providers import CODEX_ADAPTER
from telegram_bot.core.services.tmux_spawn import file_size, spawn_tmux_sync
from telegram_bot.core.services.tmux_state import TmuxSessionState, _normalize_state_dict
from telegram_bot.core.types import ChannelKey


def _transcript_path(cwd: str, session_id: str) -> Path:
    """Re-resolve `transcript_path` through `tmux_manager`'s namespace.

    Tests patch `telegram_bot.core.services.tmux_manager.transcript_path` to
    route the resurrect branch at a fake file; looking up the symbol through
    the facade module on every call makes those patches effective here. A
    direct import from `tui.paths` would capture the un-patched reference.
    """
    from telegram_bot.core.services import tmux_manager as _tm

    return _tm.transcript_path(cwd, session_id)  # type: ignore[attr-defined]


def _state_transcript_path(state: TmuxSessionState) -> Path | None:
    if state.provider == "codex":
        if not state.session_id:
            return None
        path = CODEX_ADAPTER.transcript_path_for_state(
            cwd=state.cwd,
            session_id=state.session_id,
            transcript_path=state.transcript_path,
        )
        if path is not None:
            state.transcript_path = str(path)
        return path
    if not state.session_id:
        return None
    return _transcript_path(state.cwd, state.session_id)


if TYPE_CHECKING:
    from telegram_bot.core.services.tmux_manager import TmuxManager

logger = logging.getLogger(__name__)


def _topic_base_mcp_config(
    channel_key: ChannelKey,
    *owners: object | None,
) -> tuple[bool, str | None]:
    """Return the currently configured topic MCP profile, if one exists.

    Persisted ``state.base_mcp_config`` is only a cache of what was true when
    the tmux session was created. On bot restart, topic_config.json is the
    source of truth: otherwise old long-lived sessions keep resurrecting with
    stale MCP profiles after a config deploy.
    """
    thread_id = channel_key[1]
    if thread_id is None:
        return False, None

    for owner in owners:
        if owner is None:
            continue
        topic_config = getattr(owner, "_topic_config", None)
        if topic_config is None:
            getter = getattr(owner, "get_topic_config", None)
            topic_config = getter() if callable(getter) else None
        get_topic = getattr(topic_config, "get_topic", None)
        if not callable(get_topic):
            continue
        try:
            topic = get_topic(thread_id)
        except Exception:
            logger.warning("Failed to read topic MCP config for %s", channel_key, exc_info=True)
            continue
        mcp_config = getattr(topic, "mcp_config", None)
        if isinstance(mcp_config, str) and mcp_config:
            return True, mcp_config
        return True, None
    return False, None


def _ensure_runtime_mcp_config(
    *,
    state: TmuxSessionState,
    channel_key: ChannelKey,
    session_manager: object | None,
    manager: object | None = None,
) -> None:
    if session_manager is None:
        return
    settings = getattr(session_manager, "_settings", None)
    project_root = getattr(settings, "project_root", None)
    default_getter = getattr(session_manager, "default_mcp_config_path", None)
    default_mcp = str(default_getter()) if callable(default_getter) else ""
    has_topic_config, current_topic_mcp = _topic_base_mcp_config(
        channel_key, session_manager, manager
    )
    base_mcp_config = (
        current_topic_mcp or default_mcp
        if has_topic_config
        else state.base_mcp_config or state.mcp_config or default_mcp
    )
    if has_topic_config:
        state.base_mcp_config = base_mcp_config or None
    state.mcp_config = ensure_bot_runtime_mcp_config(
        base_mcp_config=base_mcp_config or None,
        channel_key=channel_key,
        runtime_path=Path(state.session_dir) / "mcp.runtime.json",
        project_root=project_root,
    )


def build_resume_startup_cmd(
    provider: str,
    *,
    cwd: str | Path,
    session_id: str,
    mode: str,
    mcp_config: str | None,
    model: str | None,
    session_manager: object,
) -> list[str]:
    """Build provider-specific TUI resume argv."""
    if provider == "codex":
        return CODEX_ADAPTER.build_tui_resume(
            cwd=str(cwd),
            session_id=session_id,
            model=model,
            mcp_config=mcp_config,
        )
    return cast(
        list[str],
        session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
            mode=mode,
            mcp_config=mcp_config or "",
            resume_session_id=session_id,
        ),
    )


def restore_all(
    manager: TmuxManager,
    session_manager: object | None = None,
) -> dict[ChannelKey, TmuxSessionState]:
    """Load persisted sessions with runner_version awareness.

    Behavior matrix (matches tech-spec Decision 10 + Task 2 field):

      Live tmux + runner_version == "tui-v1"  → reattach.
      Live tmux + runner_version != "tui-v1"  → warn + skip (legacy
        stream-json session from pre-migration runner; startup scan in
        `__main__.py` surfaces it to the user separately).
      Dead tmux + tui-v1 + session_id + transcript exists
                                             → respawn with --resume.
      Dead tmux + legacy                     → drop.
      Dead tmux + tui-v1 w/o session_id or transcript → skip.
    """
    if not manager._state_store.exists():
        return {}

    raw: dict[str, Any] = manager._state_store.load_raw()
    if not raw:
        return {}

    restored: dict[ChannelKey, TmuxSessionState] = {}
    for key_str, data in raw.items():
        try:
            chat_id_str, thread_str = key_str.split(":", 1)
            channel_key: ChannelKey = (
                int(chat_id_str),
                int(thread_str) if thread_str != "None" else None,
            )
            # Must normalize BEFORE constructing — otherwise the dataclass
            # default "tui-v1" would silently overwrite the legacy marker
            # for old state.json entries missing runner_version.
            state = TmuxSessionState(**_normalize_state_dict(data))
            alive = manager._tmux_alive(state.session_name)
            rv = state.runner_version

            is_claude_tui = rv in {"tui-v1", "claude-tui-v1"} and state.provider == "claude"
            is_codex_tui = rv == "codex-tui-v1" and state.provider == "codex"
            is_supported_tui = is_claude_tui or is_codex_tui

            if alive and is_supported_tui:
                _ensure_runtime_mcp_config(
                    state=state,
                    channel_key=channel_key,
                    session_manager=session_manager,
                    manager=manager,
                )
                manager._sessions[channel_key] = state
                restored[channel_key] = state
                logger.info(
                    "Restored tmux session %s for channel %s",
                    state.session_name,
                    channel_key,
                )
            elif alive and not is_supported_tui:
                logger.warning(
                    "TUI_MIGRATION: orphan legacy session %s (runner_version=%s), skip",
                    state.session_name,
                    rv,
                )
                continue
            elif (
                not alive
                and is_supported_tui
                and state.session_id
                and session_manager is not None
                and (transcript := _state_transcript_path(state)) is not None
                and transcript.exists()
            ):
                _ensure_runtime_mcp_config(
                    state=state,
                    channel_key=channel_key,
                    session_manager=session_manager,
                    manager=manager,
                )
                # Resurrecting a dead session: transcript exists, so CC
                # must be told to --resume (not --session-id, which would
                # exit with "already in use").
                startup_cmd = build_resume_startup_cmd(
                    state.provider,
                    cwd=state.cwd,
                    session_id=state.session_id,
                    mode=state.mode,
                    mcp_config=state.mcp_config,
                    model=state.model,
                    session_manager=session_manager,
                )
                if not spawn_tmux_sync(
                    name=state.session_name,
                    session_dir=Path(state.session_dir),
                    cwd=state.cwd,
                    startup_cmd=startup_cmd,
                ):
                    logger.warning(
                        "Failed to resurrect tmux %s for channel %s",
                        state.session_name,
                        channel_key,
                    )
                    continue
                # `state.offset` came from state.json — it's the byte
                # position the previous run had successfully pushed to
                # Telegram. Trust it. If the bot crashed in the 10s
                # window between two `_save_state` ticks, a small replay
                # is preferable to silently dropping events the user
                # never saw. CC only appends, so persisted offset can
                # only equal or trail EOF — except in the pathological
                # case where the transcript was truncated out of band,
                # in which case we clamp to EOF to avoid seeking past it.
                transcript_size = file_size(transcript)
                if state.offset > transcript_size:
                    logger.warning(
                        "restore_all: persisted offset %d > transcript size %d for %s;"
                        " clamping to EOF",
                        state.offset,
                        transcript_size,
                        state.session_name,
                    )
                    state.offset = transcript_size
                manager._sessions[channel_key] = state
                restored[channel_key] = state
                logger.info(
                    "Resurrected tmux session %s for channel %s (CC session_id=%s)",
                    state.session_name,
                    channel_key,
                    state.session_id,
                )
            else:
                logger.info(
                    "Tmux session %s dead or unresurrectable "
                    "(runner_version=%s, session_id=%s), skipping",
                    state.session_name,
                    rv,
                    state.session_id,
                )
        except Exception:
            logger.warning("Failed to restore tmux session for key %s", key_str, exc_info=True)

    manager._save_state()
    return restored


async def resume_tails(
    manager: TmuxManager,
    on_event_factory: Callable[[ChannelKey], Callable[[StreamEvent], Awaitable[None] | None]],
) -> None:
    """Start recovery tails for all alive sessions on bot startup.

    Called once after all services are ready. Starts a persistent watcher
    for every live tmux session regardless of whether the transcript has
    pending data yet — CC may be mid-Bash-command and not have written
    anything by the time the bot restarts. The tail exits when CC sends
    a result_message, when a new user message cancels it, or when the
    tmux session dies.
    """
    for channel_key, state in list(manager._sessions.items()):
        # Route through `manager._tmux_alive` so tests that patch it
        # (`patch.object(mgr, "_tmux_alive", ...)`) can simulate transient
        # dead/alive transitions without reaching real tmux.
        if not manager._tmux_alive(state.session_name):
            continue
        if not state.session_id:
            continue
        output_path = _state_transcript_path(state)
        if output_path is None:
            continue

        on_event = on_event_factory(channel_key)
        cancel_event = asyncio.Event()
        manager._cancel_events[channel_key] = cancel_event
        asyncio.create_task(  # noqa: RUF006
            run_recovery_tail(manager, channel_key, state, output_path, on_event, cancel_event)
        )
        logger.info("Started recovery tail for channel %s (offset=%d)", channel_key, state.offset)


async def run_recovery_tail(
    manager: TmuxManager,
    channel_key: ChannelKey,
    state: TmuxSessionState,
    output_path: Path,
    on_event: Callable[[StreamEvent], Awaitable[None] | None],
    cancel_event: asyncio.Event,
) -> None:
    """Drain pending output and continue tailing until CC finishes.

    No idle_exit_sec — the tail runs until CC sends result_message, a new
    user message cancels it via cancel_event, or the tmux session dies.
    This handles the case where CC is mid-Bash-command at restart and
    hasn't written to the transcript yet when resume_tails runs.
    """
    try:
        _result_text, _new_session_id = await manager._tail_until_done(
            output_path,
            state,
            on_event,
            cancel_event,
            idle_exit_sec=None,
        )
        # Same invariant as send_stream: _tail_until_done advances
        # state.offset incrementally from bytes it actually consumed.
        # Clobbering here with file_size would skip any events written
        # between tail's last read and this return (cancel race, tmux
        # death after last poll), silently dropping them on next resume.
        manager._save_state()
    except Exception:
        logger.warning("Recovery tail failed for channel %s", channel_key, exc_info=True)
    finally:
        logger.info(
            "Recovery tail ended for channel %s (cancelled=%s, offset=%d)",
            channel_key,
            cancel_event.is_set(),
            state.offset,
        )
        if manager._cancel_events.get(channel_key) is cancel_event:
            manager._cancel_events.pop(channel_key, None)
        await manager.close_buffer(channel_key)
