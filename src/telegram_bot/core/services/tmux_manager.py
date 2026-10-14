"""TmuxManager — persistent Claude Code TUI sessions inside tmux.

Facade over the helper modules:

- `tmux_state.py`   — dataclass, atomic JSON state store (with write-lock),
                      orphan scan.
- `tmux_spawn.py`   — async / sync tmux spawn, pane-width query, aliveness
                      check, session-name builder. All subprocess calls in
                      async paths are wrapped in `asyncio.to_thread` (Wave 3
                      B4 fix).
- `tmux_modal_watchdog.py` — background task that posts idle-modal alerts.
- `tmux_recovery.py`       — bot-startup restore / resume_tails / recovery
                              tail loop.

Runtime contract (Wave 2 TUI):
  - `_spawn_tmux` → `tmux new-session -d -s <name> claude --session-id <uuid>
     --dangerously-skip-permissions ...` + `await_prompt_ready`.
  - Transcript path is derived via `tui.paths.transcript_path(cwd, session_id)`
    and polled for existence against a shared 30 s clock (Decision 7).
  - Writes to CC go through `tui.send_keys.send_text_to_tmux`; Escape / /clear
    are issued as separate `subprocess.run(["tmux", "send-keys", ...])`
    invocations with list-args (no shell=True).
  - `send_direct` and `send_stream` both route user prompts through
    `_safe_send_and_enter` (Wave 3 B1 fix) — capture → send-keys -l →
    poll-until-seen → pre-Enter re-capture → Enter. A modal-blocked prompt
    triggers a Telegram alert and the helper returns False; callers roll
    back the "Thinking..." UI.
  - `clear_context` kills the tmux session and respawns CC with a fresh
    UUID4 passed via `--session-id`.
  - `switch_session` returns bool — False when the target transcript is
    missing on disk.
  - `restore_all` / `resume_tails` live in `tmux_recovery.py`.

All `subprocess.run` invocations use list-args to defeat command injection.
`subprocess`, `capture_pane`, `send_text_to_tmux`, `send_enter`,
`await_prompt_ready` are imported at module level so tests can continue to
patch them via `telegram_bot.core.services.tmux_manager.<name>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, replace
from enum import Enum
from pathlib import Path
from typing import Literal, cast

from telegram_bot.core.messages import t
from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config
from telegram_bot.core.services.claude import Mode, StreamEvent
from telegram_bot.core.services.process_cleanup import cleanup_tmux_runtime, runtime_diagnostics
from telegram_bot.core.services.providers import (
    CODEX_ADAPTER,
    CodexTranscriptSnapshot,
    agent_env_prefix,
)
from telegram_bot.core.services.tail_runner import TailRunner
from telegram_bot.core.services.tmux_modal_watchdog import (
    ModalWatchdog,
)
from telegram_bot.core.services.tmux_modal_watchdog import (
    send_modal_alert as _send_modal_alert_impl,
)
from telegram_bot.core.services.tmux_modal_watchdog import (
    send_modal_idle_alert as _send_modal_idle_alert_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    build_resume_startup_cmd,
)
from telegram_bot.core.services.tmux_recovery import (
    restore_all as _restore_all_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    resume_tails as _resume_tails_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    run_recovery_tail as _run_recovery_tail_impl,
)
from telegram_bot.core.services.tmux_spawn import (
    MODAL_WATCHDOG_INTERVAL_SEC as _MODAL_WATCHDOG_INTERVAL_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    SPAWN_READINESS_BUDGET_SEC as _SPAWN_READINESS_BUDGET_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    TMUX_NEW_SESSION_RETRY_DELAY_SEC as _TMUX_NEW_SESSION_RETRY_DELAY_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    TMUX_NEW_SESSION_TRANSIENT_ERRORS as _TMUX_NEW_SESSION_TRANSIENT_ERRORS,
)
from telegram_bot.core.services.tmux_spawn import (
    file_size as _file_size_fn,
)
from telegram_bot.core.services.tmux_spawn import (
    make_session_name,
    sanitized_tmux_environment,
    spawn_tmux_sync,
)
from telegram_bot.core.services.tmux_spawn import (
    query_pane_width as _query_pane_width,
)
from telegram_bot.core.services.tmux_spawn import (
    tmux_alive as _tmux_alive_fn,
)
from telegram_bot.core.services.tmux_state import (
    StateStore,
    TmuxSessionState,
    _normalize_state_dict,  # noqa: F401 — re-exported for legacy test imports
    scan_orphan_tmux_sessions,
)
from telegram_bot.core.services.tmux_state import (
    peek_saved_session as _peek_saved_session_impl,
)
from telegram_bot.core.services.topic_config import Engine, TopicConfig
from telegram_bot.core.services.topic_runtime import (
    BotDefaults,
    TopicRuntimeConfig,
    resolve_topic_runtime_config,
)
from telegram_bot.core.tui.capture import await_prompt_ready
from telegram_bot.core.tui.modal_detect import (
    DEFAULT_SETTLE_SEC,
    capture_pane,
    claude_input_bar_content,
    codex_input_bar_content,
    codex_prompt_visible_in_pane,
    collect_diagnostic_signals,
    is_modal_present,
    prompt_visible_in_pane,
)
from telegram_bot.core.tui.paths import generate_session_uuid, transcript_path
from telegram_bot.core.tui.send_keys import (
    PASTE_MODE_CHAR_THRESHOLD,
    send_ctrl_u,
    send_enter,
    send_paste,
    send_text_to_tmux,
)
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

# How many trailing pane lines to dump alongside `BLOCKED reason=modal` /
# `RACE modal-before-enter` events. Wide enough to include the input bar,
# the bottom separator, and the modal footer if present (CC modals render
# their footer within ~3 lines of the pane bottom; idle bar sandwich is
# ~5 lines tall). 30 covers both with headroom for wrapped multi-line
# prompts without bloating journald.
_MODAL_DIAG_PANE_TAIL_LINES = 30

# Same rationale as send_keys._TMUX_CMD_TIMEOUT_SEC: a hung tmux server must
# not block callers holding the per-channel lifecycle lock forever.
_TMUX_CMD_TIMEOUT_SEC = 10.0


# Tail-loop constants live in tail_runner.py (W2.3 extraction).
_POLL_STEP_SEC = 0.05  # fine-grained poll interval inside send_direct's pane-verify loop
_ENTER_RETRY_SETTLE_SEC = 0.5
_ENTER_RETRY_LIMIT = 3

# A cold Codex start can accept the first prompt only after profile/plugin
# initialization has continued well beyond the pane-level Enter retry window.
# Keep transcript confirmation bounded, but allow enough time for the exact
# prompt record to appear before presenting an unconfirmed-delivery warning.
_CODEX_TRANSCRIPT_DISCOVERY_TIMEOUT_SEC = 60.0

# Codex `_safe_send_codex` paste-visibility budget. The user payload is
# pasted exactly once, then observed across three 1.5s polling windows.
# Re-pasting an unobserved payload is unsafe: delayed bracketed-paste events
# can drain together and duplicate the prompt in Codex's input bar.
_CODEX_PASTE_VISIBILITY_ATTEMPT_LIMIT = 3
_CODEX_PASTE_POLL_BUDGET_SEC = 1.5
_CODEX_PASTE_POLL_STEP_SEC = 0.1

# Claude `_safe_send_and_enter` paste-retry budget. Symmetric with codex
# above — covers the cold-start race observed 2026-04-26 23:22 UTC where
# a freshly spawned claude (engine switch) still warmed up its input bar
# 1+ seconds after `await_prompt_ready` returned True; a single 0.5s
# poll missed the paste and the bot mis-fired a modal_alert. With three
# 1s attempts the warm-up window is comfortably covered.
_CLAUDE_PASTE_RETRY_LIMIT = 3
_CLAUDE_PASTE_POLL_BUDGET_SEC = 1.0
_CLAUDE_PASTE_POLL_STEP_SEC = 0.1

# `_send_with_modal_guard` settle pauses. Bracketed paste lands as one
# event in Claude TUI, but the renderer needs a frame to absorb it before
# Enter — without a settle, Enter races the paste and either triggers
# nothing (paste still buffering) or submits the previous bar content.
# Long/multiline payloads need more time to render; short single-line
# get a smaller window so latency stays low.
#
# Threshold is aliased to `PASTE_MODE_CHAR_THRESHOLD` from `send_keys` —
# both name the Claude TUI paste-mode trigger; keeping them linked
# prevents drift if the TUI changes its threshold.
_GUARD_LONG_PROMPT_THRESHOLD = PASTE_MODE_CHAR_THRESHOLD
_GUARD_SETTLE_LONG_SEC = 0.5
_GUARD_SETTLE_SHORT_SEC = 0.25

# Probe-input readiness gate. After the prompt glyph (codex `>` / claude `>` markers) appears we
# still cannot trust that the underlying ratatui (or similar) event
# loop is processing paste/keypress events: codex demonstrably needs
# ~5-8s extra after the prompt is rendered to finish loading MCP
# servers, during which any paste lands silently in the pty but is
# not echoed back to the pane. The probe sends one literal `.` and
# watches the input bar grow; only then is the session declared ready.
# Backspace clears the probe afterwards.
_PROBE_CHAR = "."
_PROBE_INPUT_READY_BUDGET_SEC = 30.0
_PROBE_INPUT_READY_RENDER_WAIT_SEC = 0.3
_PROBE_INPUT_READY_RETRY_DELAY_SEC = 0.5

_TRANSCRIPT_WATCHDOG_INTERVAL_SEC = 5.0
_TRANSCRIPT_LAG_WARN_SEC = 10.0
_TRANSCRIPT_LAG_RESTART_SEC = 30.0


__all__ = [
    "SwitchResult",
    "TmuxManager",
    "TmuxSessionState",
    "scan_orphan_tmux_sessions",
]


@dataclass(frozen=True)
class SwitchResult:
    kind: Literal[
        "switched",
        "started",
        "already_on_it",
        "target_missing",
        "invalid_id",
        "spawn_failed",
        "config_write_failed",
    ]
    engine_changed: bool = False
    mode_changed: bool = False


class _SafeSendOutcome(Enum):
    """Result of guarded TUI prompt delivery."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"


class TmuxManager:
    """Manages persistent CC TUI sessions inside tmux windows."""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        session_name_prefix: str = "cc-",
    ) -> None:
        self._sessions_dir = sessions_dir
        self._session_name_prefix = session_name_prefix
        self._sessions: dict[ChannelKey, TmuxSessionState] = {}
        self._cancel_events: dict[ChannelKey, asyncio.Event] = {}
        self._is_processing: dict[ChannelKey, bool] = {}
        self._processing_turns: dict[ChannelKey, set[str]] = {}
        self._pending_processing: dict[ChannelKey, int] = {}
        # Shared spawn deadline per channel, populated by _spawn_tmux and
        # consumed by the next _tail_until_done to bound the transcript
        # poll-for-existence window inside the same 30s clock as readiness
        # (Decision 7). Cleared once the tail sees the file.
        self._spawn_deadlines: dict[ChannelKey, float] = {}
        # Channels whose most recent _spawn_tmux passed prompt-readiness but
        # failed the input probe (probe budget elapsed without the test char
        # appearing in the input bar). Set by `_spawn_tmux`, consumed by
        # `start_session` to skip kill-on-failure and register the session
        # anyway with `session_id=None`. Runtime-only — not persisted in
        # state.json. After bot restart, `restore_all` reattaches the tmux
        # session as a normal one; the modal watchdog will re-discover any
        # still-open modal on its next tick.
        self._probe_blocked: set[ChannelKey] = set()
        # LiveStatusBuffer per channel — owned by TmuxManager because the tail
        # loop outlives a single user message. Typed as object to avoid a
        # circular import on LiveStatusBuffer.
        self._buffers: dict[ChannelKey, object] = {}
        self._buffer_turn_ids: dict[ChannelKey, str | None] = {}
        self._pending_buffers: dict[ChannelKey, deque[object]] = {}
        self._buffer_lock = asyncio.Lock()
        # Live-buffer plumbing, wired in at startup via wire_live_buffer().
        self._bot: object | None = None
        self._topic_config: object | None = None
        self._recovery_on_event_factory: (
            Callable[
                [ChannelKey],
                Callable[[StreamEvent], Awaitable[bool | None] | bool | None],
            ]
            | None
        ) = None
        # Per-channel lock around send_direct's capture→send-keys→verify→Enter
        # sequence. Without it, two overlapping user messages race on shared
        # pane state.
        self._channel_locks: dict[ChannelKey, asyncio.Lock] = {}
        self._state_store = StateStore(sessions_dir / "state.json")
        # Channels whose currently-open modal has already produced a Telegram
        # alert. This is a lifecycle latch, not a pane snapshot: Codex redraws
        # timers and typed answers while one modal stays open, so byte-level
        # pane comparison turns harmless redraws into repeated chat messages.
        # Both user-initiated and watchdog-initiated alerts share the latch.
        self._modal_alerted_channels: set[ChannelKey] = set()
        # Re-resolve `_check_channel_modal` on every tick so `patch.object(
        # mgr, "_check_channel_modal", ...)` in tests affects the running
        # loop; otherwise the method captured at ModalWatchdog init would
        # shadow any later attribute replacement.
        self._modal_watchdog = ModalWatchdog(
            check_channel=lambda key: self._check_channel_modal(key),
            channels_snapshot=lambda: list(self._sessions.keys()),
        )
        self._transcript_watchdog_task: asyncio.Task[None] | None = None
        self._transcript_lag_since: dict[ChannelKey, float] = {}
        self._transcript_lag_warned: set[ChannelKey] = set()
        self._transcript_last_offsets: dict[ChannelKey, int] = {}
        self._codex_update_service: object | None = None

    # --- Backwards-compatible accessors ---

    @property
    def _state_path(self) -> Path:
        """Path to the persisted state.json (read-only).

        Kept as a property for tests/callers that still access
        `mgr._state_path` directly — the actual I/O goes through
        `self._state_store`.
        """
        return self._state_store.path

    @property
    def _modal_watchdog_task(self) -> asyncio.Task[None] | None:
        """Expose the watchdog task for tests. Read-only in production."""
        return self._modal_watchdog._task

    @_modal_watchdog_task.setter
    def _modal_watchdog_task(self, value: asyncio.Task[None] | None) -> None:
        self._modal_watchdog._task = value

    @property
    def _send_locks(self) -> dict[ChannelKey, asyncio.Lock]:
        """Compatibility alias for older tests; use _channel_locks in code."""
        return self._channel_locks

    def wire_live_buffer(self, *, bot: object, topic_config: object) -> None:
        """Attach the services needed to materialize LiveStatusBuffers.

        Called once at startup after Bot and TopicConfig are built.
        """
        self._bot = bot
        self._topic_config = topic_config

    def wire_codex_update_service(self, service: object) -> None:
        self._codex_update_service = service

    def live_buffer_available(self) -> bool:
        """True if wire_live_buffer() has been called — buffers can be built."""
        return self._bot is not None and self._topic_config is not None

    def get_live_bot(self) -> object | None:
        return self._bot

    def get_topic_config(self) -> object | None:
        return self._topic_config

    # --- Public API ---

    def get_buffer(self, channel_key: ChannelKey) -> object | None:
        """Return the current LiveStatusBuffer for a channel, if any."""
        return self._buffers.get(channel_key)

    async def queue_buffer(self, channel_key: ChannelKey, new_buffer: object) -> None:
        """Queue a placeholder until the transcript confirms the next turn."""
        async with self._buffer_lock:
            self._pending_buffers.setdefault(channel_key, deque()).append(new_buffer)

    async def activate_next_buffer(
        self,
        channel_key: ChannelKey,
        turn_id: str | None,
    ) -> None:
        """Give the next queued placeholder to a confirmed provider turn."""
        async with self._buffer_lock:
            pending = self._pending_buffers.get(channel_key)
            new_buffer = pending.popleft() if pending else None
            if pending is not None and not pending:
                self._pending_buffers.pop(channel_key, None)
            if new_buffer is None:
                if channel_key in self._buffers:
                    self._buffer_turn_ids[channel_key] = turn_id
                return
            old = self._buffers.get(channel_key)
            self._buffers[channel_key] = new_buffer
            self._buffer_turn_ids[channel_key] = turn_id
        if old is not None and old is not new_buffer:
            close = getattr(old, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def discard_next_buffer(self, channel_key: ChannelKey) -> None:
        """Close one queued placeholder when the confirmed turn is not live."""
        async with self._buffer_lock:
            pending = self._pending_buffers.get(channel_key)
            buffer = pending.popleft() if pending else None
            if pending is not None and not pending:
                self._pending_buffers.pop(channel_key, None)
        if buffer is not None:
            close = getattr(buffer, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def discard_last_buffer(self, channel_key: ChannelKey) -> None:
        """Discard the newest candidate when its prompt delivery failed."""
        async with self._buffer_lock:
            pending = self._pending_buffers.get(channel_key)
            buffer = pending.pop() if pending else None
            if pending is not None and not pending:
                self._pending_buffers.pop(channel_key, None)
        if buffer is not None:
            close = getattr(buffer, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def set_buffer(
        self,
        channel_key: ChannelKey,
        new_buffer: object,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Install a new LiveStatusBuffer, closing any existing one atomically.

        Atomic under self._buffer_lock so the tail's sender never ends up
        with a mix of old-and-new buffer references when the user fires a
        second prompt while CC is still working on the first.
        """
        async with self._buffer_lock:
            old = self._buffers.get(channel_key)
            self._buffers[channel_key] = new_buffer
            self._buffer_turn_ids[channel_key] = turn_id
        if old is not None:
            close = getattr(old, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def close_buffer(
        self,
        channel_key: ChannelKey,
        turn_id: str | None = None,
    ) -> None:
        """Close and forget the buffer for a channel. Idempotent."""
        async with self._buffer_lock:
            owner = self._buffer_turn_ids.get(channel_key)
            if turn_id is not None and owner is not None and owner != turn_id:
                return
            old = self._buffers.pop(channel_key, None)
            self._buffer_turn_ids.pop(channel_key, None)
            pending = (
                list(self._pending_buffers.pop(channel_key, deque())) if turn_id is None else []
            )
        for buffer in ([old] if old is not None else []) + pending:
            close = getattr(buffer, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def close_active_buffer(self, channel_key: ChannelKey) -> None:
        """Close only the current turn's buffer, preserving queued candidates."""
        async with self._buffer_lock:
            old = self._buffers.pop(channel_key, None)
            self._buffer_turn_ids.pop(channel_key, None)
        if old is not None:
            close = getattr(old, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    def is_processing(self, channel_key: ChannelKey) -> bool:
        """True if CC is actively processing a prompt for this channel."""
        return self._is_processing.get(channel_key, False)

    def mark_prompt_pending(self, channel_key: ChannelKey) -> None:
        self._pending_processing[channel_key] = self._pending_processing.get(channel_key, 0) + 1
        self._is_processing[channel_key] = True

    def handle_turn_event(self, channel_key: ChannelKey, event: StreamEvent) -> None:
        """Update processing ownership from normalized provider turn events."""
        turn_id = event.turn_id
        turns = self._processing_turns.setdefault(channel_key, set())
        if event.type == "turn_start":
            if turn_id is not None:
                turns.add(turn_id)
            pending = self._pending_processing.get(channel_key, 0)
            if pending > 1:
                self._pending_processing[channel_key] = pending - 1
            else:
                self._pending_processing.pop(channel_key, None)
        elif event.type == "turn_end":
            if turn_id is not None:
                turns.discard(turn_id)
        elif turn_id is not None:
            # Recovery may resume after the persisted turn_start boundary.
            turns.add(turn_id)

        if not turns:
            self._processing_turns.pop(channel_key, None)
        self._is_processing[channel_key] = bool(
            self._processing_turns.get(channel_key) or self._pending_processing.get(channel_key, 0)
        )

    def clear_processing(self, channel_key: ChannelKey) -> None:
        self._is_processing.pop(channel_key, None)
        self._processing_turns.pop(channel_key, None)
        self._pending_processing.pop(channel_key, None)

    def is_active(self, channel_key: ChannelKey) -> bool:
        """True if this channel has a live tmux session."""
        if channel_key not in self._sessions:
            return False
        state = self._sessions[channel_key]
        return self._tmux_alive(state.session_name)

    def active_session_count(self) -> int:
        """Number of channels with a tracked tmux session.

        Best-effort context for the health watchdog's alerts — counts known
        session state without probing tmux, so it stays cheap on the poll
        cadence.
        """
        return len(self._sessions)

    def has_live_provider(
        self,
        provider: str,
        *,
        exclude_channel: ChannelKey | None = None,
    ) -> bool:
        """True when any tracked live tmux session belongs to ``provider``."""
        for key, state in self._sessions.items():
            if exclude_channel is not None and key == exclude_channel:
                continue
            if state.provider != provider:
                continue
            if self._tmux_alive(state.session_name):
                return True
        return False

    def get_session_id(self, channel_key: ChannelKey) -> str | None:
        """Return current CC session_id for a channel."""
        state = self._sessions.get(channel_key)
        return state.session_id if state else None

    @staticmethod
    def expected_epoch(state: TmuxSessionState) -> str:
        """8-hex epoch for inline-keyboard callback_data binding.

        When the session has a real CC session_id, the first 8 hex chars
        are the keyboard epoch (as before). When session_id is None — the
        codex cold-start case where input is blocked by a startup-modal
        and codex hasn't yet written its session_meta jsonl — derive a
        stable 8-hex synthetic epoch from the tmux session_name. Same
        format as a real epoch, passes the `[0-9a-f]{8}` regex in
        `tail_keyboard.parse_tail_callback`. The synthetic epoch is
        deterministic per channel, so a `/tui` keyboard built before the
        real session_id materialises stays valid across re-renders; once
        the real session_id arrives, the old synthetic keyboard becomes
        stale (epoch mismatch) and the user is prompted to call /tui
        again — same UX as after `/new`.
        """
        if state.session_id:
            return state.session_id[:8]
        # Non-cryptographic — just a stable derivation per session_name.
        # `usedforsecurity=False` silences Bandit B324.
        return hashlib.sha1(state.session_name.encode(), usedforsecurity=False).hexdigest()[:8]

    def get_expected_epoch(self, channel_key: ChannelKey) -> str | None:
        """Public helper for callback handlers: epoch for an active session.

        Returns None when there is no registered session for the channel.
        For registered sessions, falls back to a synthetic epoch when
        the underlying CC session_id is not yet known (startup-modal
        blocked codex cold-start). See `expected_epoch` for details.
        """
        state = self._sessions.get(channel_key)
        return self.expected_epoch(state) if state else None

    @staticmethod
    def _pane_has_modal(state: TmuxSessionState, pane: str) -> bool:
        """Apply the provider-specific modal detector to one pane snapshot."""
        if state.provider == "codex":
            return CODEX_ADAPTER.is_modal_present(pane)
        return is_modal_present(pane)

    def observe_tui_pane(
        self,
        channel_key: ChannelKey,
        expected_epoch: str,
        pane: str,
    ) -> None:
        """End the current modal lifecycle after a current non-modal snapshot.

        Inline TUI callbacks capture panes outside the manager lifecycle lock.
        Bind that observation to the callback epoch so a stale keyboard cannot
        clear the alert latch for a replacement tmux session.
        """
        state = self._sessions.get(channel_key)
        if state is None or self.expected_epoch(state) != expected_epoch:
            return
        if not self._pane_has_modal(state, pane):
            self._modal_alerted_channels.discard(channel_key)

    def get_provider_model(self, channel_key: ChannelKey) -> tuple[str | None, str | None]:
        """Return (provider, model) for the live tmux session, or (None, None).

        Authoritative source for ``record_message`` in the tmux flow: the
        ``TmuxSessionState`` is set atomically by ``start_session`` /
        ``switch_session`` to the engine that actually owns the pane, so it
        cannot drift the way ``SessionManager._sessions[key].engine`` does
        between an engine-switch and the next ``_get_session()`` call.
        """
        state = self._sessions.get(channel_key)
        if state is None:
            return None, None
        return state.provider, state.model

    def get_session_snapshot(
        self, channel_key: ChannelKey
    ) -> tuple[str, str | None, str | None] | None:
        """Atomic (session_id, provider, model) snapshot, or None if no session.

        Single dict lookup eliminates the TOCTOU window between separate
        ``get_session_id`` and ``get_provider_model`` calls. Use this from
        record-message callers (tmux flow): if the session is wiped between
        the two calls, a split lookup can record a tmux-originating session_id
        with the wrong provider — exactly the failure mode this fix exists to
        prevent. Returns ``None`` (not a tuple of Nones) so callers express
        the missing-session check with one branch.
        """
        state = self._sessions.get(channel_key)
        if state is None or state.session_id is None:
            return None
        return state.session_id, state.provider, state.model

    def get_active_session_id(self, channel_key: ChannelKey) -> str | None:
        """Return current TUI session_id for picker UX."""
        return self.get_session_id(channel_key)

    def get_session_name(self, channel_key: ChannelKey) -> str | None:
        """Return current tmux session-name for a channel (e.g. `cc-1-0`)."""
        state = self._sessions.get(channel_key)
        return state.session_name if state else None

    def peek_saved_session(self, channel_key: ChannelKey, cwd: str) -> str | None:
        """Return a safely-resumable session_id from state.json, or None.

        See `tmux_state.peek_saved_session` for the full guard set. Does
        not mutate state — safe to call on every user message.
        """
        return _peek_saved_session_impl(self._state_store, channel_key, cwd)

    async def start_session(
        self,
        channel_key: ChannelKey,
        *,
        mode: Mode,
        cwd: str,
        mcp_config: str,
        chat_id: int,
        session_manager: object,
        resume_session_id: str | None = None,
        provider: str = "claude",
        model: str | None = None,
    ) -> bool:
        """Create (or resume) a tmux session with a persistent CC TUI process.

        Returns True when this call actually spawned tmux. Returns False when
        another concurrent lifecycle path already started a live session while
        this call was waiting for the per-channel lock.
        """
        async with self._get_channel_lock(channel_key):
            state = self._sessions.get(channel_key)
            if state and self._tmux_alive(state.session_name):
                logger.info(
                    "start_session no-op for %s; live tmux session already exists",
                    channel_key,
                )
                return False
            await self._start_session_unlocked(
                channel_key,
                mode=mode,
                cwd=cwd,
                mcp_config=mcp_config,
                chat_id=chat_id,
                session_manager=session_manager,
                resume_session_id=resume_session_id,
                provider=provider,
                model=model,
            )
            return True

    async def _start_session_unlocked(
        self,
        channel_key: ChannelKey,
        *,
        mode: Mode,
        cwd: str,
        mcp_config: str,
        chat_id: int,
        session_manager: object,
        resume_session_id: str | None = None,
        provider: str = "claude",
        model: str | None = None,
    ) -> None:
        """Create tmux state. Caller must hold the per-channel lifecycle lock."""
        self._ensure_state_store_writable()
        name = self._make_name(channel_key)
        session_dir = self._sessions_dir / name
        base_mcp_config = mcp_config
        mcp_config = self._ensure_runtime_mcp_config(
            channel_key=channel_key,
            base_mcp_config=base_mcp_config,
            session_dir=session_dir,
            session_manager=session_manager,
        )
        transcript_abs: str | None = None
        if provider == "codex":
            if resume_session_id is None:
                await self._maybe_auto_update_codex(channel_key)
            if resume_session_id is not None:
                session_id = resume_session_id
                startup_cmd = CODEX_ADAPTER.build_tui_resume(
                    cwd=cwd,
                    session_id=resume_session_id,
                    model=model,
                    mcp_config=mcp_config,
                )
                current_state = self._sessions.get(channel_key)
                saved_path = CODEX_ADAPTER.transcript_path_for_state(
                    cwd=cwd,
                    session_id=resume_session_id,
                    transcript_path=current_state.transcript_path if current_state else None,
                )
                initial_offset = self._file_size(saved_path) if saved_path else 0
                transcript_abs = str(saved_path) if saved_path else None
            else:
                session_id = None
                startup_cmd = CODEX_ADAPTER.build_tui_start(
                    cwd=cwd,
                    model=model,
                    mcp_config=mcp_config,
                )
                initial_offset = 0
        elif resume_session_id is not None:
            session_id = resume_session_id
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=mode,
                mcp_config=mcp_config,
                resume_session_id=resume_session_id,
                model=model,
            )
            # Seek past all events already in the transcript — they were
            # delivered to Telegram in the previous run. offset=0 would
            # re-emit every historical event and flood the user with duplicates.
            initial_offset = self._file_size(transcript_path(cwd, session_id))
        else:
            session_id = generate_session_uuid()
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=mode,
                mcp_config=mcp_config,
                session_id_new=session_id,
                model=model,
            )
            initial_offset = 0

        state = TmuxSessionState(
            session_name=name,
            session_dir=str(session_dir),
            session_id=session_id,
            mode=mode,
            cwd=cwd,
            mcp_config=mcp_config,
            chat_id=chat_id,
            offset=initial_offset,
            runner_version="codex-tui-v1" if provider == "codex" else "claude-tui-v1",
            provider=provider,
            model=model,
            transcript_path=transcript_abs,
            base_mcp_config=base_mcp_config,
        )

        # User-facing loading status: codex/claude TUI cold-start can take
        # 5-8 seconds (MCP server load + ratatui input handler wiring).
        # Without a visible signal the user thinks the bot is hung and
        # spams retries — which we then have to dedupe with paste-retry
        # double-checks. A loading message with edit-on-completion is
        # cheap UX that closes the perception gap.
        loading_msg = await self._post_engine_loading_message(channel_key, provider)

        try:
            await self._spawn_tmux(
                name=name,
                session_dir=session_dir,
                cwd=cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=provider,
            )
        except Exception as exc:
            await self._edit_engine_loading_message(
                loading_msg,
                t("ui.engine_start_failed", engine=provider, exc=str(exc)[:200]),
            )
            raise

        # `_spawn_tmux` may have succeeded structurally (tmux up + prompt
        # ready) but the input probe failed — typically because a startup
        # modal is blocking the input handler. In that case tmux is left
        # alive (see _spawn_tmux) and the channel is flagged in
        # `_probe_blocked`. Register the session anyway so /tui passes
        # `is_active(key)` and the modal watchdog sees it; pick a
        # different loading-message text so the user knows to open /tui.
        is_probe_blocked = channel_key in self._probe_blocked
        if is_probe_blocked:
            await self._edit_engine_loading_message(
                loading_msg,
                t("ui.engine_started_input_blocked", engine=provider),
            )
        else:
            await self._edit_engine_loading_message(
                loading_msg, t("ui.engine_ready", engine=provider)
            )

        self._sessions[channel_key] = state
        self._modal_alerted_channels.discard(channel_key)
        self._save_state()
        # Now that the session is registered, drop the probe-blocked flag.
        # Future spawns (clear_context / new) start fresh; the flag only
        # bridges the in-progress _spawn_tmux → start_session handoff.
        self._probe_blocked.discard(channel_key)
        logger.info(
            "Started tmux session %s for channel %s "
            "(resume=%s, session_id=%s, offset=%d, probe_blocked=%s)",
            name,
            channel_key,
            resume_session_id is not None,
            session_id,
            initial_offset,
            is_probe_blocked,
        )

    async def _maybe_auto_update_codex(self, channel_key: ChannelKey) -> None:
        service = self._codex_update_service
        if service is None:
            return
        run_auto = getattr(service, "run_auto", None)
        if run_auto is None:
            return
        try:
            result = await run_auto(
                active_check=lambda: self.has_live_provider("codex", exclude_channel=channel_key)
            )
        except Exception:
            logger.warning("Codex auto-update failed before tmux spawn", exc_info=True)
            return
        status = getattr(result, "status", None)
        if status == "already_running":
            raise RuntimeError("Codex update is already running; try again when it finishes")
        if status not in {"skipped_cooldown", "blocked_active_sessions", "disabled"}:
            logger.info("Codex auto-update before tmux spawn finished: status=%s", status)

    async def _spawn_tmux(
        self,
        *,
        name: str,
        session_dir: Path,
        cwd: str,
        startup_cmd: list[str],
        channel_key: ChannelKey | None = None,
        provider: str = "claude",
    ) -> None:
        """Spawn a tmux session running `claude` TUI and await prompt readiness.

        - Kills any previous tmux session with the same name.
        - Records a shared 30s deadline used by both `await_prompt_ready`
          and the subsequent transcript poll-for-existence (Decision 7).
        - Wave 3 B4: wraps all sync `subprocess.run` in `asyncio.to_thread`
          so the tmux server handshake no longer blocks the event loop
          (20-80 ms on idle, seconds under load).
        - On readiness-timeout `await_prompt_ready` already kills the
          session; we translate that to `RuntimeError` for the caller.
        """
        session_dir.mkdir(parents=True, exist_ok=True)
        # Clear any leftover probe-blocked flag from a previous spawn on
        # this channel before we start fresh. Other callers of
        # `_spawn_tmux` (clear_context, switch_session, _swap_session_id)
        # don't read the flag themselves, so without this discard a
        # stale flag from a prior session could leak into the next
        # `start_session` and falsely claim "blocked" when the probe
        # actually succeeded this time around.
        if channel_key is not None:
            self._probe_blocked.discard(channel_key)
        await asyncio.to_thread(
            cleanup_tmux_runtime,
            session_name=name,
            channel_key=channel_key,
            runtime_path=str(session_dir / "mcp.runtime.json"),
        )

        spawn_start = time.monotonic()
        deadline = spawn_start + _SPAWN_READINESS_BUDGET_SEC

        sanitized_startup = startup_cmd
        if startup_cmd[:2] != ["env", "-i"]:
            sanitized_startup = [*agent_env_prefix(binary=startup_cmd[0]), *startup_cmd]
        new_session_argv = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            "200",
            "-y",
            "50",
            *sanitized_startup,
        ]
        tmux_env = await asyncio.to_thread(
            sanitized_tmux_environment,
            run=subprocess.run,
        )
        result = await asyncio.to_thread(
            subprocess.run,
            new_session_argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=tmux_env,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # When `kill-session` removed the last session on the server,
            # tmux shuts the server down (exit-empty on); a racing
            # `new-session` sees "server exited unexpectedly". Retry once.
            if any(marker in stderr for marker in _TMUX_NEW_SESSION_TRANSIENT_ERRORS):
                logger.warning(
                    "tmux new-session transient failure for %s (%r); retrying once",
                    name,
                    stderr,
                )
                await asyncio.sleep(_TMUX_NEW_SESSION_RETRY_DELAY_SEC)
                result = await asyncio.to_thread(
                    subprocess.run,
                    new_session_argv,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=tmux_env,
                )
                stderr = result.stderr.strip()
            if result.returncode != 0:
                raise RuntimeError(f"tmux new-session failed: {stderr}")

        remaining = max(deadline - time.monotonic(), 0.0)
        if provider == "codex":
            ready = await self._await_codex_prompt_ready(name, timeout=remaining)
        else:
            ready = await await_prompt_ready(name, timeout=remaining, clock=time.monotonic)
        if not ready:
            with contextlib.suppress(subprocess.SubprocessError):
                await asyncio.to_thread(
                    subprocess.run,
                    ["tmux", "kill-session", "-t", f"={name}"],
                    capture_output=True,
                    timeout=_TMUX_CMD_TIMEOUT_SEC,
                )
            raise RuntimeError("CC TUI start timeout")

        # Second readiness gate: the prompt glyph (codex `>` / claude `>` markers) is visible, but
        # is the input handler actually wired up? Codex in particular
        # needs ~5-8s after the glyph appears to finish loading MCP
        # servers; sending a paste during that window is silently lost.
        # Probe with one dot and wait for it to land in the input bar.
        get_bar_fn = codex_input_bar_content if provider == "codex" else claude_input_bar_content
        ready_input = await self._probe_input_ready(name, get_input_bar_fn=get_bar_fn)
        if not ready_input:
            # Don't kill tmux. A failed probe is the strongest universal
            # signal that input is blocked — almost always a startup
            # modal (codex update prompt, trust dialog, accept-terms,
            # any future codex/claude release with a new modal). The
            # text-based whitelist in `is_modal_present` cannot keep up
            # with new modals, but the empirical "the dot never landed"
            # signal is universal. Keep tmux alive so the user can see
            # the pane through `/tui` and dismiss the modal manually;
            # the modal watchdog will also discover it on the next tick.
            # `start_session` checks this set and registers the session
            # in `_sessions` with `session_id=None` so /tui passes its
            # is_active guard.
            if channel_key is not None:
                self._probe_blocked.add(channel_key)
            logger.warning(
                "TUI_IO: probe_failed_session_kept session=%s channel=%s",
                name,
                channel_key,
            )

        if channel_key is not None:
            # Hand the remaining budget to the next _tail_until_done so its
            # transcript poll-for-existence shares the same clock.
            self._spawn_deadlines[channel_key] = deadline

    async def _await_codex_prompt_ready(self, session_name: str, timeout: float) -> bool:
        """Codex TUI readiness: wait for the input prompt without fallback Enter."""
        deadline = time.monotonic() + timeout
        trust_handled = False
        while time.monotonic() < deadline:
            try:
                pane = await capture_pane(session_name)
            except (OSError, subprocess.SubprocessError):
                return False
            if (
                not trust_handled
                and "Do you trust the contents of this directory?" in pane
                and "1. Yes, continue" in pane
            ):
                await asyncio.to_thread(
                    subprocess.run,
                    ["tmux", "send-keys", "-t", f"={session_name}:", "1", "Enter"],
                    capture_output=True,
                    check=False,
                )
                trust_handled = True
                await asyncio.sleep(0.5)
                continue
            if CODEX_ADAPTER.is_prompt_ready(pane):
                return True
            await asyncio.sleep(0.5)
        with contextlib.suppress(subprocess.SubprocessError):
            await asyncio.to_thread(
                subprocess.run,
                ["tmux", "kill-session", "-t", f"={session_name}"],
                capture_output=True,
                timeout=_TMUX_CMD_TIMEOUT_SEC,
            )
        return False

    async def _probe_input_ready(
        self,
        session_name: str,
        *,
        get_input_bar_fn: Callable[[str], str | None],
    ) -> bool:
        """Verify TUI input handler is ready — send ONE probe char, then
        poll until it appears in the input bar (or timeout).

        Background: codex/claude render the prompt glyph (`>` / `>`)
        seconds before the input handler is wired up. Anything we paste
        before the handler is alive sits silently in the pty buffer
        until the TUI eventually drains it — sometimes 5-8 seconds
        later. Pasting MULTIPLE probe chars during that window leads
        to all of them landing at once when the TUI wakes up
        (regression observed 2026-04-26 23:36 UTC: `.тест` in input bar
        after probe-with-retries).

        The fix: paste exactly one probe character. Then poll the input
        bar every `_PROBE_INPUT_READY_RETRY_DELAY_SEC` seconds checking
        if the bar changed. When change is observed → probe landed →
        send ONE Backspace to wipe it → ready.

        If the change never comes within `_PROBE_INPUT_READY_BUDGET_SEC`
        (30s default), the TUI is presumed dead/stuck — return False so
        the caller kills the session.

        `get_input_bar_fn` is provider-specific: pass
        `codex_input_bar_content` for codex, `claude_input_bar_content`
        for claude. Logic is provider-agnostic.
        """
        try:
            pane_before = await capture_pane(session_name)
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "TUI_IO: probe capture-pane (baseline) failed session=%s",
                session_name,
            )
            return False
        bar_before = get_input_bar_fn(pane_before) or ""

        # One paste — never repeat. Repetition stacks dots in the pty
        # buffer that all land at once when the TUI wakes.
        try:
            await send_paste(session_name, _PROBE_CHAR)
        except (OSError, subprocess.SubprocessError):
            logger.warning("TUI_IO: probe send_paste failed session=%s", session_name)
            return False

        deadline = time.monotonic() + _PROBE_INPUT_READY_BUDGET_SEC
        polls = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(_PROBE_INPUT_READY_RETRY_DELAY_SEC)
            polls += 1
            try:
                pane_after = await capture_pane(session_name)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: probe capture-pane (poll) failed session=%s",
                    session_name,
                )
                # Best-effort cleanup: try to wipe the dot in case it
                # eventually lands. If capture is broken, send-keys may
                # be too — swallow errors.
                await self._cleanup_probe_residue(session_name, 1)
                return False
            bar_after = get_input_bar_fn(pane_after) or ""

            if bar_before != bar_after:
                # Probe landed. One paste -> one Backspace.
                await self._cleanup_probe_residue(session_name, 1)
                logger.info(
                    "TUI_IO: TUI ready (probe accepted) session=%s polls=%d",
                    session_name,
                    polls,
                )
                return True

        # Timeout: the dot never showed up. Try to wipe in case it
        # arrives late after kill.
        await self._cleanup_probe_residue(session_name, 1)

        logger.warning(
            "TUI_IO: TUI probe timeout session=%s budget=%.1fs polls=%d",
            session_name,
            _PROBE_INPUT_READY_BUDGET_SEC,
            polls,
        )
        return False

    async def _cleanup_probe_residue(self, session_name: str, probes_sent: int) -> None:
        """Send `Backspace` once per probe paste so accumulated dots are
        wiped before we hand the input bar back to the user.

        Why exactly `probes_sent` keypresses: each successful probe
        round appended one `_PROBE_CHAR` to the input bar (the
        placeholder eviction on the first round just replaces the hint
        with the dot - still one char). Sending Backspace x N restores
        the bar to its pre-probe shape. Failures are cosmetic - at
        worst the user sees a leading `.` in their next message; we
        log and move on rather than fail the readiness check on the
        cleanup side-effect.
        """
        if probes_sent <= 0:
            return
        try:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    session_name,
                    *(["BSpace"] * probes_sent),
                ],
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "TUI_IO: probe Backspace cleanup failed session=%s probes=%d",
                session_name,
                probes_sent,
            )

    def is_tailing(self, channel_key: ChannelKey) -> bool:
        """True if a tail loop is already running for this channel."""
        return channel_key in self._cancel_events

    @staticmethod
    def _claude_input_changed(pane_before: str, pane_after: str) -> bool:
        """True when Claude's input bar changed without verified delivery.

        A changed bar means the paste likely affected the TUI, but the stricter
        delivery guard could not prove it is fresh. Re-pasting in that state can
        duplicate user input, so the send path stops and surfaces an alert.
        """
        before_bar = claude_input_bar_content(pane_before)
        after_bar = claude_input_bar_content(pane_after)
        return after_bar is not None and after_bar != before_bar

    async def _send_with_modal_guard(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        """Simplified Claude send pipeline: modal pre-check → paste → settle
        → modal recheck → Enter.

        Replaces the paste-verify retry loop (`_safe_send_and_enter` legacy
        path) for Claude sessions. The retry loop's `prompt_visible_in_pane`
        check produced false BLOCKED on every Claude TUI re-skin: 4 fixes in
        2 days on 2026-04-23..24, plus the 2026-05-07 incident with stacked
        paste residue from a prior BLOCKED. Empirical work in
        `work/reference/tgcc-server.js` (Oleg's bot) shows that paste
        verification is unnecessary when bracketed-paste delivery is used —
        a single settle pause between paste and Enter is sufficient.

        Defence remaining: `is_modal_present` is checked twice — before the
        paste and after — to ensure Enter never confirms a modal dialog.
        That is the load-bearing safety guarantee; the rest of the legacy
        verification was working around its own false positives.

        Contract:
          True  → Enter was sent.
          False → `_send_modal_alert` already posted a user-facing alert.

        Lock is taken by the caller (send_direct / send_stream); this method
        must not re-acquire it (would deadlock).
        """
        session_name = state.session_name
        logger.info(
            "TUI_IO: send_with_modal_guard session=%s len=%d",
            session_name,
            len(prompt),
        )

        # Step 1 — pre-send modal check. A modal here means Claude is
        # waiting on a dialog (model picker, /usage, trust, auto-compact);
        # pasting+Enter would silently confirm a menu item.
        pane_before = await capture_pane(session_name)
        if is_modal_present(pane_before):
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_before,
                reason="claude_modal_before_send",
            )
            return False

        # A successful non-modal observation closes any previously alerted
        # lifecycle. If another modal appears while this prompt is being
        # pasted, it is a new event and must produce its own alert.
        self._modal_alerted_channels.discard(channel_key)

        # Step 2 — bracketed paste, no Enter yet.
        try:
            await send_text_to_tmux(session_name, prompt, submit_enter=False)
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "TUI_IO: send_with_modal_guard send_text failed session=%s",
                session_name,
                exc_info=True,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_before,
                reason="claude_send_error",
            )
            return False

        # Step 3 — settle. Bracketed paste arrives as a single event but
        # the TUI needs a frame to absorb it; without this Enter races the
        # paste. Long/multiline payloads need more time to render the
        # `[Pasted text #N]` chip / wrapped lines.
        settle = (
            _GUARD_SETTLE_LONG_SEC
            if len(prompt) > _GUARD_LONG_PROMPT_THRESHOLD or "\n" in prompt
            else _GUARD_SETTLE_SHORT_SEC
        )
        await asyncio.sleep(settle)

        # Step 4 — post-paste modal check. Closes the race where a modal
        # popped between the pre-check and now (auto-compact prompt, MCP
        # auth, idle hooks). Cleanup with Ctrl+U so the orphaned paste
        # does not stack onto the next message's input bar — that was the
        # 2026-05-07 incident class.
        pane_after_paste = await capture_pane(session_name)
        if is_modal_present(pane_after_paste):
            try:
                await send_ctrl_u(session_name)
            except (OSError, subprocess.SubprocessError):
                # Best-effort — log and continue to alert. User must not
                # be left in silence even if cleanup itself fails.
                logger.warning(
                    "TUI_IO: send_with_modal_guard C-u cleanup failed session=%s",
                    session_name,
                    exc_info=True,
                )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after_paste,
                reason="claude_modal_after_send",
            )
            return False

        # Step 5 — Enter. No retry: a single Enter is sufficient on
        # bracketed-paste delivery (Oleg's bot uses one Enter; our Enter-
        # retry loop was working around its own false-detection of
        # "prompt didn't clear", not a real Enter delivery issue).
        try:
            await send_enter(session_name)
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "TUI_IO: send_with_modal_guard send_enter failed session=%s",
                session_name,
                exc_info=True,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after_paste,
                reason="claude_enter_send_error",
            )
            return False

        logger.info(
            "TUI_IO: send_with_modal_guard delivered session=%s settle=%.2fs",
            session_name,
            settle,
        )
        return True

    async def _safe_send_outcome(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> _SafeSendOutcome:
        """Return the guarded delivery outcome without hiding uncertainty."""
        if state.provider == "codex":
            return await self._safe_send_codex(channel_key, state, prompt)
        delivered = await self._send_with_modal_guard(channel_key, state, prompt)
        return _SafeSendOutcome.DELIVERED if delivered else _SafeSendOutcome.BLOCKED

    async def _safe_send_and_enter(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        """Compatibility wrapper for callers that need a boolean result.

        The streaming path uses `_safe_send_outcome` directly so it can prove
        an uncertain fresh Codex delivery from the newly-created transcript.
        Other callers receive the existing bool contract and a conservative
        unconfirmed-delivery warning.

        `send-keys Enter` on a modal would confirm the selected dialog
        item — potentially approving a shell command or switching the
        model. That risk dominates all trade-offs here.
        """
        session_name = state.session_name
        outcome = await self._safe_send_outcome(channel_key, state, prompt)
        if outcome is _SafeSendOutcome.UNCERTAIN:
            if state.session_id is not None:
                # send_direct is used only for a live, already-tailed
                # session. A successful Enter with no modal/tmux error is
                # safer to hand to that authoritative transcript tail than
                # to tell the user to resend and risk duplicate execution.
                logger.info(
                    "TUI_IO: codex direct delivery awaiting transcript session=%s",
                    session_name,
                )
                return True
            pane = await capture_pane(state.session_name)
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane,
                reason="enter_delivery_unconfirmed",
            )
        return outcome is _SafeSendOutcome.DELIVERED

        # Legacy Claude paste-
        # retry / Enter-retry loop below is unreachable dead code, kept on
        # purpose for one iteration so a `git revert` of the dispatch line
        # restores the old behaviour without a textual conflict.
        #
        # TODO(2026-05-15): if no rollback incidents, remove the dead block
        # below (everything in this method after this return), the helper
        # `_send_enter_until_prompt_clears`, the static `_claude_input_changed`,
        # the orphaned `session_name` local on the previous line, the
        # `_CLAUDE_PASTE_RETRY_LIMIT` / `_CLAUDE_PASTE_POLL_*` constants, and
        # the three direct-invocation tests of `_send_enter_until_prompt_clears`
        # in tests/test_tmux_wave3_fixes.py (lines 131, 164, 200) which only
        # pin the unreachable behaviour.
        baseline_captured_at = time.monotonic()
        pane_before = await capture_pane(session_name)
        if is_modal_present(pane_before):
            await self._send_modal_alert(
                channel_key, state, prompt, pane_before, reason="claude_modal_before_send"
            )
            return False

        # Paste-with-retry loop. Symmetric with `_safe_send_codex`: a
        # freshly spawned claude can still be warming up after
        # `await_prompt_ready` reports True (observed 2026-04-26 23:22
        # UTC after engine switch — the first paste landed in the input
        # bar 1+ seconds late, well past the original single 0.5s poll).
        # Three attempts, each polling for paste visibility within
        # `_CLAUDE_PASTE_POLL_BUDGET_SEC`. A modal popping mid-paste
        # short-circuits to a modal alert; only after all retries fail
        # do we declare the paste lost.
        pane_after = pane_before
        delivered = False
        for paste_attempt in range(1, _CLAUDE_PASTE_RETRY_LIMIT + 1):
            # Double-check before re-pasting (see codex counterpart for
            # the full incident note): if claude finally rendered the
            # previous paste between attempts, sending another one would
            # leave a duplicate copy in the input bar.
            if paste_attempt > 1:
                pane_recheck = await capture_pane(session_name)
                if is_modal_present(pane_recheck):
                    logger.info(
                        "TUI_IO: claude modal raced into pane between paste attempts "
                        "session=%s attempt=%d",
                        session_name,
                        paste_attempt,
                    )
                    await self._send_modal_alert(
                        channel_key,
                        state,
                        prompt,
                        pane_recheck,
                        reason="claude_modal_during_paste",
                    )
                    return False
                if prompt_visible_in_pane(pane_before, pane_recheck, prompt):
                    logger.info(
                        "TUI_IO: claude previous paste landed late session=%s attempt=%d",
                        session_name,
                        paste_attempt,
                    )
                    pane_after = pane_recheck
                    delivered = True
                    break
                if self._claude_input_changed(pane_before, pane_recheck):
                    logger.info(
                        "TUI_IO: claude input changed without verified delivery "
                        "session=%s attempt=%d; refusing repeat paste",
                        session_name,
                        paste_attempt,
                    )
                    pane_after = pane_recheck
                    break

            try:
                await send_text_to_tmux(session_name, prompt, submit_enter=False)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: send -l failed session=%s attempt=%d (reason=send-keys-error)",
                    session_name,
                    paste_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_before, reason="claude_send_error"
                )
                return False

            # Guarantee at least one capture even when budget is zero
            # (unit tests patch the budget to avoid spinning the poll
            # loop in real time; without do-while semantics they would
            # never observe the paste landing).
            deadline = time.monotonic() + _CLAUDE_PASTE_POLL_BUDGET_SEC
            modal_seen = False
            while True:
                await asyncio.sleep(_CLAUDE_PASTE_POLL_STEP_SEC)
                pane_after = await capture_pane(session_name)
                if is_modal_present(pane_after):
                    modal_seen = True
                    break
                if prompt_visible_in_pane(pane_before, pane_after, prompt):
                    delivered = True
                    break
                if self._claude_input_changed(pane_before, pane_after):
                    logger.info(
                        "TUI_IO: claude input changed without verified delivery "
                        "session=%s attempt=%d; refusing repeat paste",
                        session_name,
                        paste_attempt,
                    )
                    break
                if time.monotonic() >= deadline:
                    break

            if modal_seen:
                logger.info(
                    "TUI_IO: send BLOCKED session=%s reason=claude_modal_during_paste attempt=%d",
                    session_name,
                    paste_attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_after,
                    reason="claude_modal_during_paste",
                )
                return False
            if delivered:
                break

            logger.info(
                "TUI_IO: claude paste not yet visible session=%s attempt=%d; retrying",
                session_name,
                paste_attempt,
            )

        if not delivered:
            logger.info(
                "TUI_IO: send BLOCKED session=%s len=%d reason=claude_paste_not_visible",
                session_name,
                len(prompt),
            )
            elapsed_ms = int((time.monotonic() - baseline_captured_at) * 1000)
            pane_width = await _query_pane_width(session_name)
            signals = collect_diagnostic_signals(
                pane_before,
                pane_after,
                prompt,
                elapsed_ms=elapsed_ms,
                pane_width=pane_width,
            )
            pane_tail = "\n".join(pane_after.splitlines()[-_MODAL_DIAG_PANE_TAIL_LINES:])
            logger.info(
                "TUI_IO: BLOCKED diag session=%s signals=%s pane_tail=\n%s",
                session_name,
                signals,
                pane_tail,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after or pane_before,
                reason="claude_paste_not_visible",
            )
            return False

        # Pre-Enter re-capture — closes the narrow race where a modal
        # pops AFTER our verify but BEFORE we hit Enter.
        pane_pre_enter = await capture_pane(session_name)
        if not prompt_visible_in_pane(pane_before, pane_pre_enter, prompt):
            logger.info(
                "TUI_IO: send RACE session=%s reason=modal-before-enter",
                session_name,
            )
            elapsed_ms = int((time.monotonic() - baseline_captured_at) * 1000)
            pane_width = await _query_pane_width(session_name)
            signals = collect_diagnostic_signals(
                pane_before,
                pane_pre_enter,
                prompt,
                elapsed_ms=elapsed_ms,
                pane_width=pane_width,
            )
            pane_tail = "\n".join(pane_pre_enter.splitlines()[-_MODAL_DIAG_PANE_TAIL_LINES:])
            logger.info(
                "TUI_IO: RACE diag session=%s signals=%s pane_tail=\n%s",
                session_name,
                signals,
                pane_tail,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_pre_enter or pane_after,
                reason="claude_modal_before_enter",
            )
            return False

        return await self._send_enter_until_prompt_clears(
            channel_key,
            state,
            prompt,
            pane_before,
            pane_pre_enter,
        )

    async def _send_enter_until_prompt_clears(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
        pane_before: str,
        pane_pre_enter: str,
    ) -> bool:
        """Press Enter until the accepted input bar clears or a real modal appears."""
        session_name = state.session_name
        pane_current = pane_pre_enter
        for attempt in range(1, _ENTER_RETRY_LIMIT + 1):
            if attempt > 1:
                pane_before_retry = await capture_pane(session_name)
                if not pane_before_retry:
                    await self._send_modal_alert(
                        channel_key,
                        state,
                        prompt,
                        pane_current,
                        reason="claude_enter_retry_capture_empty",
                    )
                    return False
                if is_modal_present(pane_before_retry):
                    await self._send_modal_alert(
                        channel_key,
                        state,
                        prompt,
                        pane_before_retry,
                        reason="claude_modal_before_enter_retry",
                    )
                    return False
                pane_current = pane_before_retry
            try:
                await send_enter(session_name)
            except (OSError, subprocess.SubprocessError):
                logger.warning("TUI_IO: send Enter failed session=%s", session_name)
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_current,
                    reason="claude_enter_send_error",
                )
                return False

            await asyncio.sleep(_ENTER_RETRY_SETTLE_SEC)
            pane_after_enter = await capture_pane(session_name)
            if not pane_after_enter:
                logger.warning("TUI_IO: send Enter capture failed session=%s", session_name)
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_current,
                    reason="claude_enter_capture_empty",
                )
                return False
            if is_modal_present(pane_after_enter):
                logger.info(
                    "TUI_IO: send Enter opened modal session=%s attempt=%d",
                    session_name,
                    attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_after_enter,
                    reason="claude_modal_after_enter",
                )
                return False
            if (
                self._claude_queued_message_visible(pane_after_enter)
                and not self._claude_queued_message_visible(pane_current)
                and self._claude_queue_contains_prompt(pane_after_enter, prompt)
            ):
                logger.info(
                    "TUI_IO: send Enter accepted into Claude queue session=%s attempt=%d",
                    session_name,
                    attempt,
                )
                return True
            if not prompt_visible_in_pane(pane_before, pane_after_enter, prompt):
                if attempt > 1:
                    logger.info(
                        "TUI_IO: send Enter accepted after retry session=%s attempt=%d",
                        session_name,
                        attempt,
                    )
                return True
            if attempt < _ENTER_RETRY_LIMIT:
                logger.info(
                    "TUI_IO: send Enter did not submit session=%s attempt=%d; retrying",
                    session_name,
                    attempt,
                )
            pane_current = pane_after_enter

        logger.warning(
            "TUI_IO: send Enter exhausted retries session=%s attempts=%d",
            session_name,
            _ENTER_RETRY_LIMIT,
        )
        await self._send_modal_alert(
            channel_key,
            state,
            prompt,
            pane_current,
            reason="claude_enter_did_not_clear_after_retries",
        )
        return False

    async def _safe_send_codex(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> _SafeSendOutcome:
        """Codex-specific send policy: Enter-only.

        Algorithm (post-2026-04-26 simplification — Tab branches removed):

          1. capture pane_before, abort if a modal is already up.
          2. paste exactly once, then poll across 3 visibility windows
             for the prompt or `[Pasted Content]` chip to appear in the
             input bar; check for a modal between observations.
          3. capture pane_pre_enter, abort if a modal raced in or the
             current prompt/chip is no longer visible.
          4. up to 3 Enter attempts: send Enter, settle, capture; abort
             on modal_after_enter; succeed on input-bar cleared OR a
             queue marker that names this prompt.

        Every abort path posts an alert with a `reason` keyword that names
        the failure shape — those reasons grep cleanly in the TUI_ALERT_AUDIT
        log. A paste-visibility timeout is reported as unknown delivery,
        because a delayed PTY paste could still land after the final poll.

        Tab is *not* used here. Empirical 2026-04-26 22:13 UTC test on
        a busy codex confirmed Enter queues a follow-up identically to
        Tab. Tab's only old purpose — `[Pasted Content N chars]` chip
        expansion — is also obsolete now that bracketed paste (commit
        8d1363a4) collapses correctly on the first Enter.
        """
        session_name = state.session_name
        transcript_ack_path = self._transcript_path_for_state(state)
        transcript_ack_offset = (
            self._file_size(transcript_ack_path) if transcript_ack_path is not None else None
        )

        pane_before = await capture_pane(session_name)
        if CODEX_ADAPTER.is_modal_present(pane_before):
            logger.info(
                "TUI_IO: send BLOCKED session=%s len=%d reason=modal_before_send",
                session_name,
                len(prompt),
            )
            await self._send_modal_alert(
                channel_key, state, prompt, pane_before, reason="modal_before_send"
            )
            return _SafeSendOutcome.BLOCKED

        # Same lifecycle boundary as the Claude path above. The caller holds
        # the per-channel lock, so the watchdog cannot race this transition.
        self._modal_alerted_channels.discard(channel_key)

        try:
            await send_text_to_tmux(session_name, prompt, submit_enter=False)
        except (OSError, subprocess.SubprocessError):
            logger.warning("TUI_IO: codex send-paste failed session=%s", session_name)
            await self._send_modal_alert(
                channel_key, state, prompt, pane_before, reason="paste_send_error"
            )
            return _SafeSendOutcome.BLOCKED

        pane_after = pane_before
        delivered = False
        for visibility_attempt in range(1, _CODEX_PASTE_VISIBILITY_ATTEMPT_LIMIT + 1):
            # Do-while semantics: at least one capture per attempt even
            # if budget is zero (unit tests patch budget to 0 to keep
            # them fast; we still need one observation to decide).
            deadline = time.monotonic() + _CODEX_PASTE_POLL_BUDGET_SEC
            modal_seen = False
            while True:
                await asyncio.sleep(_CODEX_PASTE_POLL_STEP_SEC)
                pane_after = await capture_pane(session_name)
                if CODEX_ADAPTER.is_modal_present(pane_after):
                    modal_seen = True
                    break
                if self._codex_delivery_visible(pane_before, pane_after, prompt):
                    delivered = True
                    break
                if time.monotonic() >= deadline:
                    break

            if modal_seen:
                logger.info(
                    "TUI_IO: send BLOCKED session=%s "
                    "reason=modal_during_paste visibility_attempt=%d",
                    session_name,
                    visibility_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_after, reason="modal_during_paste"
                )
                return _SafeSendOutcome.BLOCKED
            if delivered:
                break

            logger.info(
                "TUI_IO: codex paste not yet visible session=%s "
                "visibility_attempt=%d; continuing observation",
                session_name,
                visibility_attempt,
            )

        if not delivered:
            pane_tail = "\n".join(
                (pane_after or pane_before).splitlines()[-_MODAL_DIAG_PANE_TAIL_LINES:]
            )
            logger.warning(
                "TUI_IO: codex paste not landed session=%s attempts=%d pane_tail=\n%s",
                session_name,
                _CODEX_PASTE_VISIBILITY_ATTEMPT_LIMIT,
                pane_tail,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after or pane_before,
                reason="paste_not_landed_after_retries",
            )
            return _SafeSendOutcome.BLOCKED

        pane_pre_enter = await capture_pane(session_name)
        if CODEX_ADAPTER.is_modal_present(pane_pre_enter):
            logger.info(
                "TUI_IO: send RACE session=%s reason=modal_after_paste",
                session_name,
            )
            await self._send_modal_alert(
                channel_key, state, prompt, pane_pre_enter, reason="modal_after_paste"
            )
            return _SafeSendOutcome.BLOCKED

        if not pane_pre_enter or not self._codex_delivery_visible(
            pane_before, pane_pre_enter, prompt
        ):
            logger.info(
                "TUI_IO: send BLOCKED session=%s reason=paste_visibility_lost_before_enter",
                session_name,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_pre_enter or pane_after,
                reason="paste_visibility_lost_before_enter",
            )
            return _SafeSendOutcome.BLOCKED

        pane_current = pane_pre_enter
        for enter_attempt in range(1, _ENTER_RETRY_LIMIT + 1):
            try:
                await send_enter(session_name)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: codex send Enter failed session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_current, reason="enter_send_error"
                )
                return _SafeSendOutcome.BLOCKED

            await asyncio.sleep(_ENTER_RETRY_SETTLE_SEC)
            if (
                transcript_ack_path is not None
                and transcript_ack_offset is not None
                and CODEX_ADAPTER.has_prompt_user_message_after(
                    transcript_ack_path,
                    offset=transcript_ack_offset,
                    prompt=prompt,
                )
            ):
                logger.info(
                    "TUI_IO: codex Enter accepted via transcript ack session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                return _SafeSendOutcome.DELIVERED

            pane_after_enter = await capture_pane(session_name)
            if not pane_after_enter:
                logger.warning(
                    "TUI_IO: codex Enter capture empty session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_current,
                    reason="enter_capture_empty",
                )
                return _SafeSendOutcome.BLOCKED

            if CODEX_ADAPTER.is_modal_present(pane_after_enter):
                # A modal that surfaced AFTER our Enter is still cause to
                # alert: we cannot tell from the pane alone whether the
                # modal pre-existed (Enter just confirmed it) or codex
                # raised it as a response. The /model dialog is the
                # cautionary tale — silently confirming settings is far
                # worse than asking the user to re-send a message.
                logger.info(
                    "TUI_IO: codex Enter opened modal session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_after_enter,
                    reason="modal_after_enter",
                )
                return _SafeSendOutcome.BLOCKED

            # Codex success signal: the input bar no longer carries our
            # prompt. Both delivery shapes — direct submit on idle and
            # queue-on-busy — drop the prompt out of the codex input bar
            # (the queue ack `Messages to be submitted ...` doesn't render in
            # the input bar). Do NOT short-circuit on a queue marker
            # alone: a stale queue from a previous turn can sit in the
            # pane while our prompt still occupies the input bar — that
            # is a *retry* state, not success. The 2026-04-26
            # `test_codex_send_direct_rejects_stale_pending_queue_marker`
            # regression pins this.
            if not self._codex_prompt_still_in_input_bar(pane_after_enter, prompt):
                if enter_attempt > 1:
                    logger.info(
                        "TUI_IO: codex Enter accepted after retry session=%s attempt=%d",
                        session_name,
                        enter_attempt,
                    )
                else:
                    logger.info(
                        "TUI_IO: codex Enter accepted session=%s",
                        session_name,
                    )
                return _SafeSendOutcome.DELIVERED

            if enter_attempt < _ENTER_RETRY_LIMIT:
                logger.info(
                    "TUI_IO: codex Enter did not clear input session=%s attempt=%d; retrying",
                    session_name,
                    enter_attempt,
                )
            pane_current = pane_after_enter

        logger.warning(
            "TUI_IO: codex Enter exhausted retries session=%s attempts=%d",
            session_name,
            _ENTER_RETRY_LIMIT,
        )
        # Enter was sent successfully, so a pane that still shows the prompt
        # is not proof of rejection. Fresh Codex sessions can write the exact
        # user message to a newly-created transcript just after this retry
        # window. Let the caller perform bounded transcript discovery before
        # presenting an unconfirmed-delivery warning.
        return _SafeSendOutcome.UNCERTAIN

    @staticmethod
    def _codex_prompt_visible(pane: str, prompt: str) -> bool:
        if not prompt.strip():
            return False

        # Codex wraps long pasted input in the visible TUI pane. A token like
        # "open-source" can render as "open-\nsource", which becomes
        # "open- source" after whitespace collapse. Match against the live
        # pane tail with hyphen-wraps normalized back to a single token.
        lines = pane.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        pane_tail = "\n".join(lines[-80:])
        if prompt in pane_tail:
            return True

        normalized = " ".join(prompt.split())
        pane_normalized = " ".join(pane_tail.split())
        pane_hyphen_compact = re.sub(r"-\s+", "-", pane_normalized)

        candidates = [normalized]
        if len(normalized) > 48:
            candidates.append(normalized[:48])
        if len(normalized) > 32:
            candidates.append(normalized[-32:])

        return any(
            candidate and (candidate in pane_normalized or candidate in pane_hyphen_compact)
            for candidate in candidates
        )

    def _codex_delivery_visible(self, pane_before: str, pane_after: str, prompt: str) -> bool:
        if codex_prompt_visible_in_pane(pane_before, pane_after, prompt):
            return True
        if CODEX_ADAPTER.is_modal_present(pane_after):
            return False
        return self._codex_prompt_visible(pane_after, prompt) and not self._codex_prompt_visible(
            pane_before, prompt
        )

    @staticmethod
    def _codex_pending_after_tool_call_visible(pane: str) -> bool:
        normalized = " ".join(pane.casefold().split())
        return (
            "messages to be submitted after next tool call" in normalized
            and "press esc to interrupt and send immediately" in normalized
        )

    @staticmethod
    def _codex_queued_followup_visible(pane: str) -> bool:
        normalized = " ".join(pane.casefold().split())
        return "queued follow-up inputs" in normalized or "edit last queued message" in normalized

    @staticmethod
    def _codex_prompt_still_in_input_bar(pane: str, prompt: str) -> bool:
        bar = codex_input_bar_content(pane)
        if not bar:
            return False
        if "[pasted content" in bar.casefold():
            return True

        normalized = " ".join(prompt.split())
        bar_normalized = " ".join(bar.split())
        bar_hyphen_compact = re.sub(r"-\s+", "-", bar_normalized)
        candidates = [normalized]
        if len(normalized) > 48:
            candidates.append(normalized[:48])
        if len(normalized) > 32:
            candidates.append(normalized[-32:])
        return any(
            candidate and (candidate in bar_normalized or candidate in bar_hyphen_compact)
            for candidate in candidates
        )

    @staticmethod
    def _pane_contains_prompt_snippet(pane: str, prompt: str) -> bool:
        if not pane or not prompt.strip():
            return False
        normalized = " ".join(prompt.split())
        pane_normalized = " ".join(pane.split())
        pane_hyphen_compact = re.sub(r"-\s+", "-", pane_normalized)
        candidates = [normalized]
        if len(normalized) > 48:
            candidates.append(normalized[:48])
        if len(normalized) > 32:
            candidates.append(normalized[-32:])
        return any(
            candidate and (candidate in pane_normalized or candidate in pane_hyphen_compact)
            for candidate in candidates
        )

    @classmethod
    def _codex_queue_contains_prompt(cls, pane: str, prompt: str) -> bool:
        lines = pane.splitlines()
        for idx, line in enumerate(lines):
            line_norm = " ".join(line.casefold().split())
            if (
                "queued follow-up inputs" not in line_norm
                and "messages to be submitted after next tool call" not in line_norm
            ):
                continue
            window = "\n".join(lines[idx : idx + 24])
            if cls._pane_contains_prompt_snippet(window, prompt):
                return True
        return False

    @classmethod
    def _claude_queue_contains_prompt(cls, pane: str, prompt: str) -> bool:
        lines = pane.splitlines()
        for idx, line in enumerate(lines):
            if "press up to edit queued messages" not in " ".join(line.casefold().split()):
                continue
            window = "\n".join(lines[max(0, idx - 24) : idx + 1])
            if cls._pane_contains_prompt_snippet(window, prompt):
                return True
        return False

    @staticmethod
    def _claude_queued_message_visible(pane: str) -> bool:
        normalized = " ".join(pane.casefold().split())
        return "press up to edit queued messages" in normalized

    async def send_direct(self, channel_key: ChannelKey, prompt: str) -> bool:
        """Deliver `prompt` to the tmux TUI, verifying the input actually
        landed before sending Enter. Returns True iff Enter was sent.

        Return value drives caller cleanup: on False, the caller must
        delete the "Thinking..." placeholder and close any LiveStatusBuffer
        it spawned — otherwise both hang forever with no signal back to
        the user.
        """
        async with self._get_channel_lock(channel_key):
            state = self._sessions.get(channel_key)
            if not state:
                return False
            return await self._send_direct_locked(channel_key, state, prompt)

    async def _send_direct_locked(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        session_name = state.session_name
        logger.info("TUI_IO: send_direct session=%s len=%d", session_name, len(prompt))

        # Reuses the shared safe-send helper; on False the modal alert was
        # already posted. Setting the processing flag happens AFTER verify
        # so that a modal-blocked send doesn't leave `/mode` picker busy.
        if not await self._safe_send_and_enter(channel_key, state, prompt):
            return False

        # Enter was delivered — normalized turn boundaries now own the busy
        # flag; cancel/session teardown remains the safety cleanup.
        # Codex records an additional user_message inside the current
        # turn as a clarification, not as a queued new turn. Keep the active
        # turn as the sole processing owner so task_complete can clear it.
        if state.provider == "codex" and self._processing_turns.get(channel_key):
            self._is_processing[channel_key] = True
        else:
            self.mark_prompt_pending(channel_key)
        logger.info("TUI_IO: send_direct delivered session=%s", session_name)
        return True

    async def _poll_pane_for_prompt(
        self,
        session_name: str,
        pane_before: str,
        prompt: str,
    ) -> str:
        """Wait for the prompt to appear in the pane, up to a budget.

        Polls every `_POLL_STEP_SEC` (~50 ms) and returns as soon as the
        diff check confirms visibility — fast path on idle CC. Budget is
        `DEFAULT_SETTLE_SEC * 2.5` (~500 ms total) to stay resilient on
        slow hosts without blocking the request path. Guaranteed to make
        at least one capture attempt (do-while), so `DEFAULT_SETTLE_SEC=0`
        in tests still exercises the verify path."""
        deadline = asyncio.get_event_loop().time() + DEFAULT_SETTLE_SEC * 2.5
        while True:
            await asyncio.sleep(_POLL_STEP_SEC)
            pane_after = await capture_pane(session_name)
            if prompt_visible_in_pane(pane_before, pane_after, prompt):
                return pane_after
            if asyncio.get_event_loop().time() >= deadline:
                return pane_after

    async def _post_engine_loading_message(
        self,
        channel_key: ChannelKey,
        provider: str,
    ) -> object | None:
        """Post a "🔄 {engine} starting up" status message to the user's
        chat at the start of `start_session`. Returns the sent Message
        object so the caller can edit it on completion, or None if the
        bot is unwired (test scaffolding) or the post itself fails.

        Best-effort: any aiogram error is swallowed — failing to post a
        loading hint must not block the actual session spawn.
        """
        bot = self._bot
        if bot is None:
            return None
        try:
            message: object = await bot.send_message(  # type: ignore[attr-defined]
                chat_id=channel_key[0],
                text=t("ui.engine_starting", engine=provider),
                message_thread_id=channel_key[1],
            )
            return message
        except Exception:  # UX hint, never fatal
            logger.warning(
                "TUI_IO: failed to post engine loading message channel=%s",
                channel_key,
                exc_info=True,
            )
            return None

    async def _edit_engine_loading_message(
        self,
        sent: object | None,
        new_text: str,
    ) -> None:
        """Edit a previously posted loading message to its terminal
        state ("ready" or "failed"). No-op if `sent` is None
        (`_post_engine_loading_message` returned None) or if the edit
        fails (Telegram drop, message deleted by user, etc.)."""
        if sent is None:
            return
        edit = getattr(sent, "edit_text", None)
        if edit is None:
            return
        try:
            await edit(new_text)
        except Exception:  # UX hint, never fatal
            logger.warning(
                "TUI_IO: failed to edit engine loading message",
                exc_info=True,
            )

    async def _send_modal_alert(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
        pane: str,
        *,
        reason: str = "unspecified",
    ) -> None:
        """Thin wrapper — impl in `tmux_modal_watchdog.send_modal_alert`.

        Kept on the class so tests can `patch.object(mgr, "_send_modal_alert", ...)`.
        `reason` names the failure path that triggered the alert and gets
        logged into TUI_ALERT_AUDIT for grep-based triage.
        """
        await _send_modal_alert_impl(self, channel_key, state, prompt, pane, reason=reason)

    async def _send_modal_idle_alert(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        pane: str,
        *,
        reason: str = "modal_idle_detected",
    ) -> None:
        """Thin wrapper — impl in `tmux_modal_watchdog.send_modal_idle_alert`."""
        await _send_modal_idle_alert_impl(self, channel_key, state, pane, reason=reason)

    # --- Modal watchdog ---

    def start_modal_watchdog(self, interval_sec: float = _MODAL_WATCHDOG_INTERVAL_SEC) -> None:
        """Launch the background task that scans every active session for
        idle modals. Idempotent — calling while a task is running is a
        no-op. Skipped if live-buffer wiring is absent (tests / live-mode
        disabled) because there's no Bot to post alerts with anyway."""
        if self._bot is None:
            logger.info("TUI_IO: modal watchdog skipped — no bot wired")
            return
        self._modal_watchdog.start(interval_sec)

    async def stop_modal_watchdog(self) -> None:
        """Cancel the watchdog task and await its exit. Safe to call in
        teardown even if the task was never started."""
        await self._modal_watchdog.stop()

    def start_transcript_watchdog(
        self,
        *,
        interval_sec: float = _TRANSCRIPT_WATCHDOG_INTERVAL_SEC,
    ) -> None:
        """Launch the JSONL tail-health watchdog.

        Modal watchdog answers "is Codex waiting for a button?". This answers
        the separate production failure mode: "Codex is appending to transcript
        JSONL, but the bot's persisted offset is not moving". A stuck tail can
        still be present in `_cancel_events`, so presence alone is not a
        reliable health signal.
        """
        if self._transcript_watchdog_task is not None and not self._transcript_watchdog_task.done():
            return
        self._transcript_watchdog_task = asyncio.create_task(
            self._transcript_watchdog_loop(interval_sec)
        )

    async def stop_transcript_watchdog(self) -> None:
        task = self._transcript_watchdog_task
        self._transcript_watchdog_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _transcript_watchdog_loop(self, interval_sec: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(interval_sec)
                for channel_key in list(self._sessions.keys()):
                    try:
                        await self._check_transcript_tail_health(channel_key)
                    except Exception:
                        logger.warning(
                            "Transcript watchdog probe failed for channel %s",
                            channel_key,
                            exc_info=True,
                        )

    async def _check_transcript_tail_health(
        self,
        channel_key: ChannelKey,
        *,
        now: float | None = None,
        restart_after_sec: float = _TRANSCRIPT_LAG_RESTART_SEC,
    ) -> None:
        """Restart a stale transcript tail when JSONL grows but offset stalls."""
        now = time.monotonic() if now is None else now
        state = self._sessions.get(channel_key)
        if state is None or not state.session_id:
            self._clear_transcript_lag(channel_key)
            return
        if not self._tmux_alive(state.session_name):
            self._clear_transcript_lag(channel_key)
            return
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            self._clear_transcript_lag(channel_key)
            return

        transcript_size = self._file_size(output_path)
        lag_bytes = transcript_size - state.offset
        if lag_bytes <= 0:
            self._clear_transcript_lag(channel_key)
            self._transcript_last_offsets[channel_key] = state.offset
            return

        previous_offset = self._transcript_last_offsets.get(channel_key)
        self._transcript_last_offsets[channel_key] = state.offset
        if previous_offset is None or previous_offset != state.offset:
            self._transcript_lag_since[channel_key] = now
            self._transcript_lag_warned.discard(channel_key)
            return

        lag_since = self._transcript_lag_since.setdefault(channel_key, now)
        lag_age = now - lag_since
        tail_active = channel_key in self._cancel_events
        if lag_age >= _TRANSCRIPT_LAG_WARN_SEC and channel_key not in self._transcript_lag_warned:
            logger.warning(
                "Transcript tail lag channel=%s session=%s offset=%d file_size=%d "
                "lag_bytes=%d lag_age=%.1fs tail_active=%s",
                channel_key,
                state.session_name,
                state.offset,
                transcript_size,
                lag_bytes,
                lag_age,
                tail_active,
            )
            self._transcript_lag_warned.add(channel_key)

        effective_restart_after_sec = 0.0 if not tail_active else restart_after_sec
        if lag_age < effective_restart_after_sec:
            return
        if self._recovery_on_event_factory is None:
            return

        # Restart must not race lifecycle operations (send_stream's locked
        # delivery, recycle, kill). If one is in flight, skip this probe —
        # the next tick re-evaluates against fresh state.
        lock = self._channel_locks.get(channel_key)
        if lock is not None and lock.locked():
            return
        async with self._get_channel_lock(channel_key):
            if self._sessions.get(channel_key) is not state:
                # Session was killed/recycled between the probe and the lock.
                return
            old_cancel = self._cancel_events.get(channel_key)
            if old_cancel is not None:
                old_cancel.set()
                await self._wait_for_tail_exit(channel_key, timeout=2.5)
                if self._cancel_events.get(channel_key) is old_cancel:
                    logger.warning(
                        "Transcript watchdog replacing stale tail channel=%s session=%s: "
                        "old tail did not exit before timeout",
                        channel_key,
                        state.session_name,
                    )

            started = await self._start_recovery_tail(channel_key, state, output_path)
        if started:
            self._is_processing[channel_key] = True
            self._transcript_lag_since[channel_key] = now
            self._transcript_lag_warned.discard(channel_key)
            logger.warning(
                "Transcript watchdog restarted recovery tail channel=%s session=%s "
                "offset=%d file_size=%d lag_bytes=%d",
                channel_key,
                state.session_name,
                state.offset,
                transcript_size,
                lag_bytes,
            )

    def _clear_transcript_lag(self, channel_key: ChannelKey) -> None:
        self._transcript_lag_since.pop(channel_key, None)
        self._transcript_lag_warned.discard(channel_key)
        self._transcript_last_offsets.pop(channel_key, None)

    async def _modal_watchdog_loop(self, interval_sec: float) -> None:
        """Legacy in-class loop, delegates to ModalWatchdog.

        Kept for tests that directly instantiate the loop without going
        through start_modal_watchdog. New code should call `start_modal_watchdog`.
        """
        await self._modal_watchdog._loop(interval_sec)

    async def _check_channel_modal(self, channel_key: ChannelKey) -> None:
        """Single watchdog probe for one channel: skip when a send_direct
        is in flight, capture the pane, and post one idle-alert per continuous
        modal lifecycle. Pane redraws do not re-alert. Once the pane no longer
        shows a modal, clear the latch so a future modal can fire again."""
        lock = self._get_channel_lock(channel_key)
        if lock.locked():
            return

        # Own the lifecycle lock for the entire observation and alert send.
        # Merely sampling lock.locked() leaves a race where a session can be
        # replaced between capture and Telegram delivery.
        async with lock:
            state = self._sessions.get(channel_key)
            if state is None:
                return
            pane = await capture_pane(state.session_name)
            if not pane:
                return
            if self._pane_has_modal(state, pane):
                if channel_key in self._modal_alerted_channels:
                    return
                await self._send_modal_idle_alert(channel_key, state, pane)
            else:
                self._modal_alerted_channels.discard(channel_key)

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[bool | None] | bool | None],
    ) -> str:
        """Send user message to persistent CC TUI and tail transcript until done.

        Starts a long-running tail that streams all CC events via on_event.
        Returns empty string (results are sent immediately via result_message,
        not accumulated).

        Wave 3 B1: routes the prompt through `_safe_send_and_enter` so a
        modal-blocked session no longer blind-Enters on a dialog. On
        False, the tail is never started (CC would not emit a result
        event) and the finally block cleans up flags.
        """
        cancel_event = asyncio.Event()
        state: TmuxSessionState | None = None
        output_path: Path | None = None

        async def _on_event_with_processing(event: StreamEvent) -> bool | None:
            if event.type == "result":
                self.clear_processing(channel_key)
                return None
            ret = on_event(event)
            if asyncio.iscoroutine(ret):
                ret = await ret
            self.handle_turn_event(channel_key, event)
            return cast(bool | None, ret)

        try:
            async with self._get_channel_lock(channel_key):
                state = self._sessions.get(channel_key)
                if state is None:
                    # Session was killed between the caller's check and our
                    # lock acquisition — KeyError here used to crash the
                    # stream with no user-visible message.
                    logger.warning("TUI_IO: send_stream with no session channel=%s", channel_key)
                    ret = on_event(StreamEvent("result_message", t("ui.tmux_not_active")))
                    if asyncio.iscoroutine(ret):
                        await ret
                    return ""
                self._is_processing[channel_key] = True
                logger.info(
                    "TUI_IO: send_stream session=%s len=%d",
                    state.session_name,
                    len(prompt),
                )

                # Cancel any running recovery/previous tail before starting a new one.
                existing = self._cancel_events.get(channel_key)
                if existing:
                    existing.set()
                    # Full 2.5s grace (same as _wait_for_tail_exit default):
                    # the old 0.5s window often left the previous tail alive,
                    # and two tails on one transcript duplicate every event.
                    await self._wait_for_tail_exit(channel_key)

                codex_snapshot = None
                needs_codex_discovery = (
                    state.provider == "codex"
                    and state.session_id is None
                    and state.transcript_path is None
                )
                if needs_codex_discovery:
                    codex_snapshot = CODEX_ADAPTER.capture_tui_transcript_snapshot()

                self._cancel_events[channel_key] = cancel_event
                outcome = await self._safe_send_outcome(channel_key, state, prompt)
                if outcome is _SafeSendOutcome.BLOCKED:
                    # _safe_send_and_enter posted an alert; do not start tail.
                    return ""

                codex_discovered = False
                if outcome is _SafeSendOutcome.UNCERTAIN:
                    if needs_codex_discovery and codex_snapshot is not None:
                        try:
                            await self._locate_codex_transcript_after_send(
                                state,
                                snapshot=codex_snapshot,
                                prompt=prompt,
                            )
                        except RuntimeError:
                            pane = await capture_pane(state.session_name)
                            await self._send_modal_alert(
                                channel_key,
                                state,
                                prompt,
                                pane,
                                reason="enter_delivery_unconfirmed",
                            )
                            return ""
                        codex_discovered = True
                        logger.info(
                            "TUI_IO: codex delivery confirmed by fresh transcript session=%s",
                            state.session_name,
                        )
                    else:
                        # We successfully sent Enter and observed no modal or
                        # tmux error.  On an established Codex session the
                        # input bar can lag behind the transcript, so warning
                        # the user to resend here can duplicate a request that
                        # Codex already accepted.  Continue with the existing
                        # transcript tail; it is the authoritative turn
                        # signal for this session.
                        logger.info(
                            "TUI_IO: codex delivery awaiting existing transcript session=%s",
                            state.session_name,
                        )

                self.mark_prompt_pending(channel_key)

                if needs_codex_discovery and codex_snapshot is not None and not codex_discovered:
                    try:
                        await self._locate_codex_transcript_after_send(
                            state,
                            snapshot=codex_snapshot,
                            prompt=prompt,
                        )
                    except RuntimeError:
                        logger.warning(
                            "Codex TUI transcript discovery failed after delivery; "
                            "leaving session alive channel=%s session=%s",
                            channel_key,
                            state.session_name,
                            exc_info=True,
                        )
                        ret = on_event(
                            StreamEvent(
                                "result_message",
                                "Codex принял сообщение, но бот не смог найти transcript "
                                "для стриминга ответа. Сессия оставлена живой; открой /tui "
                                "или отправь следующее сообщение после завершения работы.",
                            )
                        )
                        if asyncio.iscoroutine(ret):
                            await ret
                        await self.close_buffer(channel_key)
                        return ""

                output_path = self._transcript_path_for_state(state)
            if output_path is None:
                target = state.session_name if state else channel_key
                logger.warning("No transcript path for %s", target)
                return ""
            _result_text, _new_session_id = await self._tail_until_done(
                output_path, state, _on_event_with_processing, cancel_event
            )

            # state.offset is maintained incrementally by _tail_until_done
            # from the bytes it actually consumed. Re-reading file_size
            # here would leap over any events written between tail's last
            # read and this point (e.g. after cancel was signalled),
            # silently dropping them on the next resume.
            self._save_state()

            # Results are sent immediately via result_message events.
            # Returning the text would cause streaming.py to send it again
            # as the "final" message, producing a duplicate.
            return ""
        finally:
            if self._cancel_events.get(channel_key) is cancel_event:
                self._cancel_events.pop(channel_key, None)
            self.clear_processing(channel_key)

    async def cancel(self, channel_key: ChannelKey) -> None:
        """Interrupt CC processing and cancel the tail loop.

        Sends Escape via `tmux send-keys` to interrupt the current CC
        operation, then unblocks the tail.

        Takes the per-channel lifecycle lock so Escape never lands in the
        middle of a prompt delivery (send_stream's locked section) or a
        recycle/clear_context mutation.
        """
        async with self._get_channel_lock(channel_key):
            await self._cancel_unlocked(channel_key)

    async def _cancel_unlocked(self, channel_key: ChannelKey) -> None:
        """Cancel body. Caller must hold the per-channel lifecycle lock."""
        state = self._sessions.get(channel_key)
        if state:
            logger.info("TUI_IO: cancel session=%s", state.session_name)
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["tmux", "send-keys", "-t", f"={state.session_name}:", "Escape"],
                    capture_output=True,
                    timeout=_TMUX_CMD_TIMEOUT_SEC,
                )
            except subprocess.SubprocessError:
                # Escape undelivered — still close the buffer and unblock the
                # tail below, otherwise the channel stays stuck in processing.
                logger.warning("TUI_IO: cancel Escape failed for session=%s", state.session_name)

        # Close buffer BEFORE signalling cancel: once the tail sees the event
        # it may return from _tail_until_done and leave the buffer's worker
        # orphaned.
        await self.close_buffer(channel_key)

        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            logger.info("Cancelled tmux tail for channel %s", channel_key)

        # Clear the processing flag unconditionally. Recovery tails and
        # send_direct callers never install _on_event_with_processing, so
        # without this clear the flag would stay True forever after /kill
        # or /cancel — and any subsequent busy-check would be stuck.
        self.clear_processing(channel_key)

        # Offset is maintained incrementally by _tail_until_done — no
        # post-cancel recompute needed.

    async def _wait_for_tail_exit(self, channel_key: ChannelKey, timeout: float = 2.5) -> None:
        """Busy-wait up to `timeout` seconds for the tail's finally block
        to pop its cancel_event from `_cancel_events`.

        Shared helper used by `clear_context` and `switch_session` — both
        mutate tmux after cancelling any in-flight tail, and must let the
        tail's finally block clean up first so the next `_tail_until_done`
        doesn't see a stale cancel_event.
        """
        iterations = max(1, int(timeout / 0.05))
        for _ in range(iterations):
            if channel_key not in self._cancel_events:
                return
            await asyncio.sleep(0.05)
        logger.warning("Timed out waiting for tail cleanup for %s", channel_key)

    async def clear_context(self, channel_key: ChannelKey, session_manager: object) -> bool:
        async with self._get_channel_lock(channel_key):
            return await self._clear_context_unlocked(channel_key, session_manager)

    async def _clear_context_unlocked(
        self, channel_key: ChannelKey, session_manager: object
    ) -> bool:
        """Reset CC context by respawning tmux with a fresh `--session-id`.

        Historical note: earlier versions sent `Escape → /clear → Enter` into
        the live CC TUI. That leaked session-id drift — CC TUI silently
        rotates its internal session_id on `/clear` and writes subsequent
        events to a new transcript jsonl, while the bot kept tailing the old
        (dead) file. Respawn keeps the UUID under the bot's control.

        Returns True if a live tmux session was found and respawned,
        False if tmux was already dead.
        """
        state = self._sessions.get(channel_key)
        if not state or not self._tmux_alive(state.session_name):
            return False
        self._ensure_state_store_writable()

        logger.info("TUI_IO: clear_context session=%s", state.session_name)

        # Close buffer before cancelling the tail — see cancel() for rationale.
        await self.close_buffer(channel_key)

        # Unblock any active tail loop, then wait for its finally block to
        # clear the cancel_event.
        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            await self._wait_for_tail_exit(channel_key)

        candidate = replace(state)
        if candidate.provider == "codex":
            candidate.base_mcp_config = self._effective_base_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config or candidate.mcp_config,
                session_manager=session_manager,
            )
            candidate.mcp_config = self._ensure_runtime_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config,
                session_dir=Path(candidate.session_dir),
                session_manager=session_manager,
            )
            new_session_id = None
            startup_cmd = CODEX_ADAPTER.build_tui_start(
                cwd=candidate.cwd,
                model=candidate.model,
                mcp_config=candidate.mcp_config,
            )
            candidate.session_id = None
            candidate.transcript_path = None
        else:
            candidate.base_mcp_config = self._effective_base_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config or candidate.mcp_config,
                session_manager=session_manager,
            )
            candidate.mcp_config = self._ensure_runtime_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config,
                session_dir=Path(candidate.session_dir),
                session_manager=session_manager,
            )
            new_session_id = generate_session_uuid()
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=candidate.mode,
                mcp_config=candidate.mcp_config,
                session_id_new=new_session_id,
                model=candidate.model,
            )
            candidate.session_id = new_session_id
            candidate.transcript_path = None

        candidate.offset = 0
        try:
            await self._spawn_tmux(
                name=candidate.session_name,
                session_dir=Path(candidate.session_dir),
                cwd=candidate.cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=candidate.provider,
            )
        except RuntimeError:
            logger.warning(
                "clear_context respawn failed for %s; preserving durable session_id=%s",
                state.session_name,
                state.session_id,
            )
            self._save_state()
            raise
        self._commit_state(state, candidate)
        self._sessions[channel_key] = state
        self._modal_alerted_channels.discard(channel_key)
        self._save_state()
        logger.info(
            "Respawned tmux session %s with fresh CC session %s",
            candidate.session_name,
            new_session_id,
        )
        return True

    async def switch_session(
        self,
        channel_key: ChannelKey,
        new_session_id: str,
        session_manager: object,
    ) -> bool:
        async with self._get_channel_lock(channel_key):
            return await self._switch_session_unlocked(channel_key, new_session_id, session_manager)

    async def _switch_session_unlocked(
        self,
        channel_key: ChannelKey,
        new_session_id: str,
        session_manager: object,
    ) -> bool:
        """Switch the tmux CC to a different session.

        Guard (Decision 13): if the target transcript file does not exist
        on disk, refuse to touch tmux/state and return False.

        Returns:
          True  — kill+spawn completed, `state.session_id == new_session_id`.
          False — target transcript missing; tmux/state unchanged.
        """
        state = self._sessions.get(channel_key)
        if not state:
            return False
        self._ensure_state_store_writable()

        # Shape-check the incoming uuid BEFORE handing it to transcript_path —
        # the helper's UUID4 assert would otherwise raise AssertionError for
        # legacy or malformed ids.
        if state.provider == "codex":
            target_transcript = self._find_codex_transcript(new_session_id, state.cwd)
        else:
            from telegram_bot.core.tui.paths import _SESSION_ID_RE

            if not _SESSION_ID_RE.fullmatch(new_session_id):
                logger.info("switch_session: malformed target session_id %r", new_session_id)
                return False
            target_transcript = transcript_path(state.cwd, new_session_id)
        if target_transcript is None:
            logger.info("switch_session: target transcript missing for %s", new_session_id)
            return False
        if not target_transcript.exists():
            logger.info("switch_session: target transcript missing for %s", new_session_id)
            return False

        # Close the buffer first so the old session's thinking page stops
        # accepting appends and has its keyboard stripped.
        await self.close_buffer(channel_key)

        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            await self._wait_for_tail_exit(channel_key)

        # Resume an existing transcript — guard above confirmed target
        # jsonl exists. `--session-id` would make CC bail with "Session
        # ID is already in use".
        candidate = replace(state)
        candidate.base_mcp_config = self._effective_base_mcp_config(
            channel_key=channel_key,
            base_mcp_config=candidate.base_mcp_config or candidate.mcp_config,
            session_manager=session_manager,
        )
        candidate.mcp_config = self._ensure_runtime_mcp_config(
            channel_key=channel_key,
            base_mcp_config=candidate.base_mcp_config,
            session_dir=Path(candidate.session_dir),
            session_manager=session_manager,
        )
        if candidate.provider == "codex":
            startup_cmd = CODEX_ADAPTER.build_tui_resume(
                cwd=candidate.cwd,
                session_id=new_session_id,
                model=candidate.model,
                mcp_config=candidate.mcp_config,
            )
        else:
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=candidate.mode,
                mcp_config=candidate.mcp_config,
                resume_session_id=new_session_id,
                model=candidate.model,
            )
        try:
            await self._spawn_tmux(
                name=candidate.session_name,
                session_dir=Path(candidate.session_dir),
                cwd=candidate.cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=candidate.provider,
            )
        except RuntimeError:
            self._save_state()
            raise
        candidate.session_id = new_session_id
        candidate.transcript_path = (
            str(target_transcript) if candidate.provider == "codex" else None
        )
        # Seed past all events already in the target transcript — offset=0
        # would re-emit every historical event through on_event.
        candidate.offset = self._file_size(target_transcript)
        self._commit_state(state, candidate)
        self._sessions[channel_key] = state
        self._modal_alerted_channels.discard(channel_key)
        self._save_state()
        logger.info(
            "Switched tmux session %s to CC session %s (offset=%d)",
            candidate.session_name,
            new_session_id,
            candidate.offset,
        )
        return True

    async def switch_or_start_session(
        self,
        channel_key: ChannelKey,
        target_session_id: str,
        target_provider: Engine,
        target_transcript_path: Path,
        *,
        session_manager: object,
        topic_config: TopicConfig,
        defaults: BotDefaults,
    ) -> SwitchResult:
        """Switch live tmux to a selected transcript, or start it if dormant."""
        async with self._get_channel_lock(channel_key):
            self._ensure_state_store_writable()
            settings = topic_config.get_topic(channel_key[1])
            runtime = resolve_topic_runtime_config(settings, defaults)
            original_runtime = runtime

            if not self._validate_session_id_shape(target_session_id, target_provider):
                return SwitchResult(kind="invalid_id")
            if not target_transcript_path.exists():
                return SwitchResult(kind="target_missing")

            thread_id = channel_key[1]
            if thread_id is None:
                return SwitchResult(kind="config_write_failed")

            captured = self._sessions.get(channel_key)
            engine_changed = target_provider != runtime.engine
            mode_changed = runtime.exec_mode != "tmux"
            captured_alive = bool(captured and self._tmux_alive(captured.session_name))
            models = settings.models if isinstance(settings.models, dict) else {}
            target_model = models.get(target_provider)
            if mode_changed and engine_changed:
                ok = await topic_config.update_engine_model_exec_mode(
                    thread_id,
                    target_provider,
                    None,
                    "tmux",
                )
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                runtime = replace(
                    runtime,
                    engine=target_provider,
                    model=target_model,
                    exec_mode="tmux",
                )
            elif mode_changed:
                ok = await topic_config.update_exec_mode(thread_id, "tmux")
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                runtime = replace(runtime, exec_mode="tmux")
            elif engine_changed:
                if models:
                    ok = await topic_config.update_engine(thread_id, target_provider)
                else:
                    ok = await topic_config.update_engine_model(thread_id, target_provider, None)
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                runtime = replace(runtime, engine=target_provider, model=target_model)

            if (
                captured_alive
                and captured
                and captured.session_id == target_session_id
                and captured.provider == target_provider
            ):
                return SwitchResult(
                    kind="already_on_it",
                    engine_changed=engine_changed,
                    mode_changed=mode_changed,
                )

            replacement_name = self._make_name(channel_key)
            if captured_alive:
                replacement_name = f"{replacement_name}-switch-{time.time_ns():x}"
            replacement_dir = self._sessions_dir / replacement_name
            runtime_mcp_config = self._ensure_runtime_mcp_config(
                channel_key=channel_key,
                base_mcp_config=runtime.mcp_config,
                session_dir=replacement_dir,
                session_manager=session_manager,
            )
            runtime_for_resume = replace(runtime, mcp_config=runtime_mcp_config)
            new_state = self._build_state_for_resume(
                channel_key=channel_key,
                runtime=runtime_for_resume,
                provider=target_provider,
                session_id=target_session_id,
                transcript_path=target_transcript_path,
            )
            new_state.session_name = replacement_name
            new_state.session_dir = str(replacement_dir)
            new_state.base_mcp_config = runtime.mcp_config
            startup_cmd = build_resume_startup_cmd(
                target_provider,
                cwd=runtime.cwd,
                session_id=target_session_id,
                mode=runtime.mode,
                mcp_config=runtime_for_resume.mcp_config,
                model=runtime.model,
                session_manager=session_manager,
            )
            try:
                await self._spawn_tmux(
                    name=new_state.session_name,
                    session_dir=Path(new_state.session_dir),
                    cwd=str(runtime.cwd),
                    startup_cmd=startup_cmd,
                    channel_key=channel_key,
                    provider=target_provider,
                )
            except RuntimeError:
                self._save_state()
                await self._rollback_topic_runtime(
                    topic_config=topic_config,
                    thread_id=thread_id,
                    runtime=original_runtime,
                )
                return SwitchResult(
                    kind="spawn_failed",
                    engine_changed=engine_changed,
                    mode_changed=mode_changed,
                )

            new_state.offset = self._file_size(target_transcript_path)
            if captured_alive and captured:
                await self.close_buffer(channel_key)
                cancel_event = self._cancel_events.get(channel_key)
                if cancel_event:
                    cancel_event.set()
                    await self._wait_for_tail_exit(channel_key)
                await self._kill_tmux_only(channel_key, captured)
            self._sessions[channel_key] = new_state
            self._modal_alerted_channels.discard(channel_key)
            self._save_state()
            if engine_changed:
                clear_provider = getattr(session_manager, "clear_provider_session", None)
                if clear_provider is not None:
                    await clear_provider(channel_key)
            return SwitchResult(
                kind="switched" if captured else "started",
                engine_changed=engine_changed,
                mode_changed=mode_changed,
            )

    async def kill(self, channel_key: ChannelKey) -> None:
        """Kill tmux session for the channel.

        Wave 3 3.5: additionally clears per-channel transient state
        (`_modal_alerted_channels`, `_channel_locks`, `_spawn_deadlines`, `_buffers`)
        so a subsequent `start_session` on the same channel starts from a
        clean slate. Without this, stale send-locks from a killed session
        could serialise unrelated work, and a stale modal-alert latch could
        suppress a new session's modal alert.
        """
        async with self._get_channel_lock(channel_key):
            await self._kill_session_unlocked(channel_key)

    async def recycle(self, channel_key: ChannelKey, session_manager: object) -> bool:
        """Restart the current tmux runtime and clean its MCP processes.

        Unlike `/new`, this does not intentionally reset conversation state.
        It resumes when the provider has a known session id; Codex sessions
        without discovered ids are restarted fresh because there is nothing
        reliable to resume.
        """
        async with self._get_channel_lock(channel_key):
            state = self._sessions.get(channel_key)
            if not state:
                return False
            self._ensure_state_store_writable()
            original = replace(state)
            await self.close_buffer(channel_key)
            event = self._cancel_events.get(channel_key)
            if event:
                event.set()
                await self._wait_for_tail_exit(channel_key)

            candidate = replace(state)
            candidate.base_mcp_config = self._effective_base_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config or candidate.mcp_config,
                session_manager=session_manager,
            )
            candidate.mcp_config = self._ensure_runtime_mcp_config(
                channel_key=channel_key,
                base_mcp_config=candidate.base_mcp_config,
                session_dir=Path(candidate.session_dir),
                session_manager=session_manager,
            )
            resume_id = candidate.session_id
            if candidate.provider == "codex":
                if resume_id:
                    startup_cmd = CODEX_ADAPTER.build_tui_resume(
                        cwd=candidate.cwd,
                        session_id=resume_id,
                        model=candidate.model,
                        mcp_config=candidate.mcp_config,
                    )
                else:
                    startup_cmd = CODEX_ADAPTER.build_tui_start(
                        cwd=candidate.cwd,
                        model=candidate.model,
                        mcp_config=candidate.mcp_config,
                    )
            elif resume_id:
                startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                    mode=candidate.mode,
                    mcp_config=candidate.mcp_config,
                    resume_session_id=resume_id,
                    model=candidate.model,
                )
            else:
                resume_id = generate_session_uuid()
                candidate.session_id = resume_id
                startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                    mode=candidate.mode,
                    mcp_config=candidate.mcp_config,
                    session_id_new=resume_id,
                    model=candidate.model,
                )

            try:
                await self._spawn_tmux(
                    name=candidate.session_name,
                    session_dir=Path(candidate.session_dir),
                    cwd=candidate.cwd,
                    startup_cmd=startup_cmd,
                    channel_key=channel_key,
                    provider=candidate.provider,
                )
            except RuntimeError:
                self._commit_state(state, original)
                self._sessions[channel_key] = state
                self._save_state()
                raise
            transcript = self._transcript_path_for_state(candidate)
            old_transcript = self._transcript_path_for_state(original)
            if not (
                transcript
                and transcript == old_transcript
                and candidate.session_id == original.session_id
            ):
                # Fresh session / new transcript — start from current EOF.
                # When the SAME transcript is resumed, keep the old offset
                # (already copied by replace): output written between the
                # tail cancel above and the restart would otherwise be
                # silently dropped; the recovery tail delivers it instead.
                candidate.offset = self._file_size(transcript) if transcript else 0
            self._commit_state(state, candidate)
            self._sessions[channel_key] = state
            self._modal_alerted_channels.discard(channel_key)
            self._save_state()
            logger.info(
                "Recycled tmux session %s for channel %s", candidate.session_name, channel_key
            )
            return True

    async def _kill_session_unlocked(self, channel_key: ChannelKey) -> None:
        # Cancel tail loop first so send_stream unblocks. Unlocked variant —
        # kill() already holds the lifecycle lock.
        await self._cancel_unlocked(channel_key)

        state = self._sessions.pop(channel_key, None)
        if state:
            if not self._state_store.remove(channel_key, reason="user_kill"):
                self._sessions[channel_key] = state
                raise RuntimeError("tmux state is not writable; refusing to kill durable session")
            await self._kill_tmux_only(channel_key, state)
            logger.info("Killed tmux session %s", state.session_name)

        # Clear per-channel transient state except the lifecycle lock. Locks
        # are intentionally stable so waiters cannot split across old/new locks.
        self._modal_alerted_channels.discard(channel_key)
        self._spawn_deadlines.pop(channel_key, None)

    async def _kill_tmux_only(self, channel_key: ChannelKey, state: TmuxSessionState) -> None:
        """Kill tmux process only; caller owns state lifecycle."""
        await asyncio.to_thread(
            cleanup_tmux_runtime,
            session_name=state.session_name,
            channel_key=channel_key,
            runtime_path=state.mcp_config,
        )

    def mcp_status_text(self, channel_key: ChannelKey) -> str:
        state = self._sessions.get(channel_key)
        if not state:
            return "No active tmux session in this topic."
        configured = self._configured_mcp_servers(state.mcp_config)
        diag = runtime_diagnostics(
            session_name=state.session_name,
            channel_key=channel_key,
            runtime_path=state.mcp_config,
            configured_servers=configured,
        )
        dupes = diag.duplicate_generations
        duplicate_lines = (
            ", ".join(f"{name}={count}" for name, count in sorted(dupes.items()))
            if dupes
            else "none"
        )
        return "\n".join(
            [
                f"topic: {channel_key[0]}:{channel_key[1]}",
                f"session: {state.session_name}",
                f"provider: {state.provider}",
                f"configured: {', '.join(configured) if configured else 'none'}",
                f"pane_pid: {diag.pane_pid or 'n/a'}",
                f"pane_sid: {diag.pane_sid or 'n/a'}",
                f"sid_processes: {len(diag.sid_processes)}",
                f"tagged_processes: {len(diag.tagged_processes)}",
                f"mcp_counts: {duplicate_lines}",
                f"rss_mb: {diag.rss_kb / 1024:.1f}",
            ]
        )

    def _configured_mcp_servers(self, mcp_config: str | None) -> tuple[str, ...]:
        if not mcp_config:
            return ()
        try:
            data = json.loads(Path(mcp_config).read_text(encoding="utf-8"))
        except Exception:
            return ()
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return ()
        return tuple(sorted(str(name) for name in servers))

    def restore_all(
        self, session_manager: object | None = None
    ) -> dict[ChannelKey, TmuxSessionState]:
        """Load persisted sessions with runner_version awareness.

        Delegates to `tmux_recovery.restore_all` — see that module for the
        full behavior matrix.
        """
        return _restore_all_impl(self, session_manager)

    async def resume_tails(
        self,
        on_event_factory: Callable[
            [ChannelKey],
            Callable[[StreamEvent], Awaitable[bool | None] | bool | None],
        ],
    ) -> None:
        """Start recovery tails for all alive sessions on bot startup.

        Delegates to `tmux_recovery.resume_tails`.
        """
        self._recovery_on_event_factory = on_event_factory
        await _resume_tails_impl(self, on_event_factory)

    async def ensure_recovery_tail(self, channel_key: ChannelKey) -> bool:
        """Start a recovery tail for manual TUI actions if no tail is active.

        `/tui` can unblock Codex/Claude by pressing Enter or selecting a modal
        option. Those outputs are not caused by `send_stream`, so without this
        hook the transcript is only drained on the next Telegram message.
        """
        if channel_key in self._cancel_events:
            return False
        state = self._sessions.get(channel_key)
        if state is None or not state.session_id:
            return False
        if not self._tmux_alive(state.session_name):
            return False
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            return False
        return await self._start_recovery_tail(channel_key, state, output_path, reason="manual")

    async def _start_recovery_tail(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        output_path: Path,
        *,
        reason: str = "watchdog",
    ) -> bool:
        if self._recovery_on_event_factory is None:
            return False

        on_event = self._recovery_on_event_factory(channel_key)
        cancel_event = asyncio.Event()
        self._cancel_events[channel_key] = cancel_event
        asyncio.create_task(  # noqa: RUF006
            _run_recovery_tail_impl(self, channel_key, state, output_path, on_event, cancel_event)
        )
        logger.info(
            "Started %s recovery tail for channel %s (offset=%d)",
            reason,
            channel_key,
            state.offset,
        )
        return True

    async def _run_recovery_tail(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        output_path: Path,
        on_event: Callable[[StreamEvent], Awaitable[bool | None] | bool | None],
        cancel_event: asyncio.Event,
    ) -> None:
        """Delegate to `tmux_recovery.run_recovery_tail`.

        Kept on the class for tests that call `mgr._run_recovery_tail(...)`
        directly (regression coverage for B2 offset preservation).
        """
        from telegram_bot.core.services.tmux_recovery import run_recovery_tail

        await run_recovery_tail(self, channel_key, state, output_path, on_event, cancel_event)

    def _spawn_tmux_sync(
        self,
        *,
        name: str,
        session_dir: Path,
        cwd: str,
        startup_cmd: list[str],
    ) -> bool:
        """Synchronous respawn used by `restore_all`. Kept on the class so
        subclasses / tests can patch it. Delegates to `tmux_spawn.spawn_tmux_sync`.
        """
        return spawn_tmux_sync(
            name=name,
            session_dir=session_dir,
            cwd=cwd,
            startup_cmd=startup_cmd,
        )

    # --- Internal helpers ---

    def _make_name(self, channel_key: ChannelKey) -> str:
        """Channel-key → tmux session name, using the configured prefix."""
        return make_session_name(channel_key, prefix=self._session_name_prefix)

    def _topic_base_mcp_config(
        self,
        channel_key: ChannelKey,
        session_manager: object | None = None,
    ) -> tuple[bool, str | None]:
        """Return whether topic config was read and its current MCP profile."""
        thread_id = channel_key[1]
        if thread_id is None:
            return False, None

        owners = (session_manager, self)
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

    def _default_mcp_config(
        self,
        session_manager: object | None = None,
    ) -> tuple[str | None, str | Path | None]:
        application_root = None
        default_mcp: str | None = None
        if session_manager is not None:
            settings = getattr(session_manager, "_settings", None)
            application_root = getattr(settings, "app_root_path", None)
            if not isinstance(application_root, str | Path):
                application_root = getattr(settings, "app_root", None) or getattr(
                    settings, "project_root", None
                )
            default_getter = getattr(session_manager, "default_mcp_config_path", None)
            if callable(default_getter):
                default_mcp = str(default_getter())
        return default_mcp, application_root

    def _effective_base_mcp_config(
        self,
        *,
        channel_key: ChannelKey,
        base_mcp_config: str | None,
        session_manager: object | None = None,
    ) -> str | None:
        has_topic_config, topic_mcp = self._topic_base_mcp_config(channel_key, session_manager)
        default_mcp, _project_root = self._default_mcp_config(session_manager)
        if has_topic_config:
            return topic_mcp or default_mcp
        return base_mcp_config or default_mcp

    def _ensure_runtime_mcp_config(
        self,
        *,
        channel_key: ChannelKey,
        base_mcp_config: str | None,
        session_dir: Path,
        session_manager: object | None = None,
    ) -> str:
        default_mcp, project_root = self._default_mcp_config(session_manager)
        has_topic_config, topic_mcp = self._topic_base_mcp_config(channel_key, session_manager)
        if has_topic_config:
            effective_base = topic_mcp or default_mcp
        else:
            effective_base = base_mcp_config or default_mcp
        return ensure_bot_runtime_mcp_config(
            base_mcp_config=effective_base or None,
            channel_key=channel_key,
            runtime_path=session_dir / "mcp.runtime.json",
            project_root=project_root,
        )

    def _get_channel_lock(self, channel_key: ChannelKey) -> asyncio.Lock:
        """Return the stable per-channel lifecycle lock. Never delete entries."""
        lock = self._channel_locks.get(channel_key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_key] = lock
        return lock

    @staticmethod
    def _validate_session_id_shape(session_id: str, provider: str) -> bool:
        if provider == "codex":
            from telegram_bot.core.tui.paths import _CODEX_SESSION_ID_RE

            return bool(_CODEX_SESSION_ID_RE.fullmatch(session_id))
        from telegram_bot.core.tui.paths import _SESSION_ID_RE

        return bool(_SESSION_ID_RE.fullmatch(session_id))

    def _build_state_for_resume(
        self,
        *,
        channel_key: ChannelKey,
        runtime: TopicRuntimeConfig,
        provider: str,
        session_id: str,
        transcript_path: Path,
    ) -> TmuxSessionState:
        name = self._make_name(channel_key)
        return TmuxSessionState(
            session_name=name,
            session_dir=str(self._sessions_dir / name),
            session_id=session_id,
            mode=runtime.mode,
            cwd=str(runtime.cwd),
            mcp_config=runtime.mcp_config or "",
            chat_id=channel_key[0],
            offset=0,
            runner_version="codex-tui-v1" if provider == "codex" else "claude-tui-v1",
            provider=provider,
            model=runtime.model,
            transcript_path=str(transcript_path) if provider == "codex" else None,
            base_mcp_config=runtime.mcp_config,
        )

    @staticmethod
    def _tmux_alive(session_name: str) -> bool:
        return _tmux_alive_fn(session_name)

    @staticmethod
    def _file_size(path: Path) -> int:
        return _file_size_fn(path)

    def _save_state(self) -> None:
        """Persist sessions via the thread-safe StateStore.

        Wave 3 B3: the store guards `tmp → os.replace` with a
        `threading.Lock`, so concurrent tail-loop saves no longer race.
        """
        self._state_store.save(self._sessions)

    def remove_state(self, channel_key: ChannelKey, *, reason: str) -> None:
        self._state_store.remove(channel_key, reason=reason)

    @staticmethod
    def _commit_state(target: TmuxSessionState, source: TmuxSessionState) -> None:
        for field in fields(TmuxSessionState):
            setattr(target, field.name, getattr(source, field.name))

    def _ensure_state_store_writable(self) -> None:
        result = self._state_store.load()
        if not result.ok:
            raise RuntimeError("tmux state is unreadable; repair state.json before tmux changes")

    @staticmethod
    async def _rollback_topic_runtime(
        *,
        topic_config: TopicConfig,
        thread_id: int,
        runtime: TopicRuntimeConfig,
    ) -> None:
        try:
            await topic_config.update_engine_model_exec_mode(
                thread_id,
                runtime.engine,
                runtime.model,
                runtime.exec_mode,
            )
        except Exception:
            logger.warning(
                "Failed to roll back topic runtime after tmux spawn failure",
                exc_info=True,
            )

    async def _locate_codex_transcript_after_send(
        self,
        state: TmuxSessionState,
        *,
        snapshot: CodexTranscriptSnapshot,
        prompt: str,
    ) -> None:
        try:
            info = await CODEX_ADAPTER.locate_tui_transcript(
                cwd=state.cwd,
                snapshot=snapshot,
                prompt=prompt,
                timeout_sec=_CODEX_TRANSCRIPT_DISCOVERY_TIMEOUT_SEC,
            )
        except Exception:
            raise RuntimeError("Codex TUI transcript discovery failed") from None
        state.session_id = info.session_id
        state.transcript_path = str(info.transcript_path)
        state.offset = info.tail_start_offset
        self._save_state()

    def _transcript_path_for_state(self, state: TmuxSessionState) -> Path | None:
        if state.provider == "codex":
            if state.transcript_path:
                return Path(state.transcript_path)
            if not state.session_id:
                return None
            return CODEX_ADAPTER.transcript_path_for_state(
                cwd=state.cwd,
                session_id=state.session_id,
                transcript_path=state.transcript_path,
            )
        if not state.session_id:
            return None
        return transcript_path(state.cwd, state.session_id)

    def _codex_transcript_for_state(self, channel_key: ChannelKey) -> Path | None:
        state = self._sessions.get(channel_key)
        if state is None or not state.session_id:
            return None
        path = CODEX_ADAPTER.transcript_path_for_state(
            cwd=state.cwd,
            session_id=state.session_id,
            transcript_path=state.transcript_path,
        )
        if path is not None:
            state.transcript_path = str(path)
        return path

    @staticmethod
    def _find_codex_transcript(session_id: str, cwd: str) -> Path | None:
        return CODEX_ADAPTER.find_tui_transcript(cwd=cwd, session_id=session_id)

    # Public alias for callers outside the class (shutdown handlers, etc.).
    # External code should not poke at `_save_state` directly.
    def persist_state(self) -> None:
        """Persist tmux session state. Safe to call from outside the class."""
        self._save_state()

    async def _tail_until_done(
        self,
        output_path: Path,
        state: TmuxSessionState,
        on_event: Callable[[StreamEvent], Awaitable[bool | None] | bool | None],
        cancel_event: asyncio.Event,
        *,
        idle_exit_sec: float | None = None,
    ) -> tuple[str, str | None]:
        """Thin wrapper around TailRunner (W2.3 extraction).

        Owns the two pieces of context that a TailRunner cannot compute on
        its own: the channel_key for the given state (for rotation WARN
        context), and the existence deadline — shared with the spawn-readiness
        budget via ``_spawn_deadlines`` (Decision 7).

        A popped deadline in the past is ignored — ``/new`` (clear_context) and
        reply-to-resume (switch_session) call ``_spawn_tmux`` too, but the next
        user message can arrive minutes later, leaving the shared clock stale
        before the tail starts.
        """
        start_time = time.monotonic()
        channel_key = self._find_channel_key(state)
        existence_deadline: float = start_time + _SPAWN_READINESS_BUDGET_SEC
        if channel_key is not None:
            popped = self._spawn_deadlines.pop(channel_key, None)
            if popped is not None and popped > start_time:
                existence_deadline = popped

        runner = TailRunner(
            channel_key=channel_key,
            state=state,
            output_path=output_path,
            on_event=on_event,
            cancel_event=cancel_event,
            save_state=self._save_state,
            tmux_alive=self._tmux_alive,
            existence_deadline=existence_deadline,
            idle_exit_sec=idle_exit_sec,
        )
        return await runner.run()

    def _find_channel_key(self, state: TmuxSessionState) -> ChannelKey | None:
        """Reverse-lookup channel_key for a given state (cheap, dict is small)."""
        for key, candidate in self._sessions.items():
            if candidate is state:
                return key
        return None
