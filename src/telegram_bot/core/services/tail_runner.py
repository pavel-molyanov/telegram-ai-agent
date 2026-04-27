"""TailRunner — reads CC transcript jsonl and fans out StreamEvents.

Extracted from ``TmuxManager._tail_until_done`` (W2.3). One instance per tail
invocation. Lifetime == one read cycle; instance state (rotation-dedup set,
backlog-warn latch) is intentionally scoped to a single run so each tail gets
a fresh dedup window.

State ownership invariant (PLAN.md W2.3 #1): ``state.session_id`` is NEVER
written from here. The tail only *observes* the transcript ``sessionId``;
writes live with ``start_session`` / ``switch_session`` / ``clear_context``.
``state.offset`` IS updated from here — via the injected ``save_state``
callback, which since W2.5 writes atomically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from telegram_bot.core.services.claude import StreamEvent
from telegram_bot.core.services.providers import CODEX_ADAPTER
from telegram_bot.core.tui.transcript import parse_transcript_event
from telegram_bot.core.types import ChannelKey


class _StateAccess(Protocol):
    """Structural slice of TmuxSessionState used by TailRunner.

    Declared as a Protocol to avoid a tmux_manager ↔ tail_runner import
    cycle (tmux_manager already imports TailRunner).

    ``offset`` must be writable — the tail advances it as lines are read.
    ``session_id`` and ``session_name`` are read-only observed fields.
    """

    session_name: str
    session_id: str | None
    offset: int
    provider: str


logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.1  # seconds between transcript jsonl reads
TAIL_TIMEOUT = 21600  # 6h — matches cc_query_timeout_sec
ALIVE_CHECK_INTERVAL = 5.0  # seconds between tmux_alive checks
OFFSET_SAVE_INTERVAL = 10.0  # seconds between periodic offset saves
ON_EVENT_SLOW_WARN_SEC = 5.0  # warn when a single on_event dispatch exceeds this
EVENT_QUEUE_BACKLOG_WARN = 500  # warn once per tail when backlog exceeds this
SENDER_DRAIN_GRACE_SEC = 30.0  # how long we wait for the sender to flush on normal exit

# Codex-only post-`task_complete` grace period. Codex emits the
# `task_complete` event when it considers the turn finished, but
# empirically (2026-04-26 incident, plan.md §"Факт 2") it can keep
# writing the final paragraph 10+ seconds *after* that — and the prior
# tail loop closed on `done=True` immediately, losing 13 seconds of
# final answer. The fix: when codex signals done, hold the tail open
# for this many seconds; any further event resets the timer; only
# silence past the deadline closes the loop.
TASK_COMPLETE_GRACE_SEC = 60.0


OnEventCallable = Callable[[StreamEvent], Awaitable[None] | None]


class TailRunner:
    """Reads one CC transcript jsonl and fans out StreamEvents via on_event.

    Constructed once per tail cycle. ``run()`` blocks until one of:

    * ``cancel_event`` is set (fast exit, drops pending events)
    * ``tmux_alive`` returns False (tmux session died)
    * a ``result`` event is observed
    * ``idle_exit_sec`` elapsed without any new lines (recovery tails)
    * 6h hard timeout

    The tail loop parses jsonl on disk and enqueues StreamEvents; a separate
    sender task invokes ``on_event`` for each queued event. This isolation
    means a slow ``on_event`` (Telegram flood-wait) cannot stall parsing.

    PLAN.md W2.3 invariants preserved:
        1. state.session_id not written — only observed.
        2. state.offset written via save_state (atomic after W2.5).
        3. Rotation WARN dedup via ``warned_rotated_sids`` set.
        4. backlog_warned single-shot per instance.
        5. sender_task cancellation ordering: queue-sentinel first, then
           cancel on cancel-path; on normal path await drain with grace.
        6. transcript_available poll-wait before main loop.
        7. tmux_alive probed on fixed interval (ALIVE_CHECK_INTERVAL).
        8. result-event enqueues result_message (if content) then result;
           tail continues (CC TUI never emits this shape — kept for parity
           with the stream-json-era behaviour to avoid silent drift).
    """

    def __init__(
        self,
        *,
        channel_key: ChannelKey | None,
        state: _StateAccess,
        output_path: Path,
        on_event: OnEventCallable,
        cancel_event: asyncio.Event,
        save_state: Callable[[], None],
        tmux_alive: Callable[[str], bool],
        existence_deadline: float,
        idle_exit_sec: float | None = None,
    ) -> None:
        self._channel_key = channel_key
        self._state = state
        self._output_path = output_path
        self._on_event = on_event
        self._cancel_event = cancel_event
        self._save_state = save_state
        self._tmux_alive = tmux_alive
        self._existence_deadline = existence_deadline
        self._idle_exit_sec = idle_exit_sec
        # Cache once — session_name is immutable for a state's lifetime
        # and every log line uses it.
        self._session_name_cached = state.session_name

        self._result_text: str = ""
        self._session_id: str | None = None
        # Rotation warn dedup — set because CC can flap A→B→A and each
        # distinct rotated sid deserves one WARN, but a repeat of the same
        # rotated sid does not.
        self._warned_rotated_sids: set[str] = set()
        self._backlog_warned: bool = False
        self._event_queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        # Codex post-task_complete grace deadline: monotonic seconds at
        # which the tail will close if no further events arrive. None
        # while the turn is still in progress; set when `task_complete`
        # is observed; pushed forward on every subsequent event so a
        # late-streaming codex doesn't lose its final paragraph.
        self._task_complete_grace_deadline: float | None = None

    async def run(self) -> tuple[str, str | None]:
        """Entry point. Returns (result_text, observed_session_id)."""
        start_time = time.monotonic()
        deadline = start_time + TAIL_TIMEOUT
        last_alive_check = start_time - ALIVE_CHECK_INTERVAL  # check immediately on first iter
        last_offset_save = start_time
        last_activity = start_time

        session_name = self._session_name()
        sender_task = asyncio.create_task(self._sender_loop())

        if not await self._wait_for_transcript(sender_task):
            return self._result_text, self._session_id

        try:
            while True:
                if self._cancel_event.is_set():
                    logger.info("Tmux tail cancelled for %s", session_name)
                    break

                if time.monotonic() > deadline:
                    logger.warning("Tmux tail timed out after %ds", TAIL_TIMEOUT)
                    break

                now = time.monotonic()
                if now - last_alive_check >= ALIVE_CHECK_INTERVAL:
                    last_alive_check = now
                    if not self._tmux_alive(session_name):
                        logger.warning("Tmux session %s died, stopping tail", session_name)
                        # Read remaining output before breaking so a race
                        # between tmux death and final transcript flush
                        # still delivers the last events.
                        lines = await asyncio.to_thread(self._read_new_lines)
                        await self._process_lines(lines)
                        break

                lines = await asyncio.to_thread(self._read_new_lines)
                if lines:
                    last_activity = time.monotonic()
                elif self._idle_exit_sec is not None and (
                    time.monotonic() - last_activity >= self._idle_exit_sec
                ):
                    logger.info(
                        "Tmux tail idle-exit for %s after %.1fs",
                        session_name,
                        self._idle_exit_sec,
                    )
                    break
                if await self._process_lines(lines):
                    break
                if (
                    self._task_complete_grace_deadline is not None
                    and time.monotonic() >= self._task_complete_grace_deadline
                ):
                    logger.info(
                        "Tmux tail task_complete grace expired session=%s",
                        session_name,
                    )
                    break

                now = time.monotonic()
                if now - last_offset_save >= OFFSET_SAVE_INTERVAL:
                    last_offset_save = now
                    self._save_state()

                await asyncio.sleep(POLL_INTERVAL)
        finally:
            # Signal sender to finish. On cancel we don't wait — drop pending
            # events so /clear and kill stay snappy. On normal exit we give
            # the sender a bounded grace period to drain, then cancel it.
            self._event_queue.put_nowait(None)
            if self._cancel_event.is_set():
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender_task
            else:
                try:
                    await asyncio.wait_for(sender_task, timeout=SENDER_DRAIN_GRACE_SEC)
                except TimeoutError:
                    logger.warning(
                        "Sender drain timed out on %s (%d events dropped)",
                        session_name,
                        self._event_queue.qsize(),
                    )
                    sender_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender_task

        return self._result_text, self._session_id

    async def _wait_for_transcript(self, sender_task: asyncio.Task[None]) -> bool:
        """Poll until output_path exists or existence_deadline elapses.

        Returns True if the file appeared, False if cancel/deadline exited us
        early. On early exit also shuts down the sender task cleanly — caller
        should return immediately after a False.
        """
        session_name = self._session_name()
        while True:
            if self._cancel_event.is_set():
                logger.info(
                    "Tmux tail cancelled before transcript existed (%s)",
                    session_name,
                )
                self._event_queue.put_nowait(None)
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender_task
                return False
            if self._output_path.exists():
                return True
            if time.monotonic() > self._existence_deadline:
                logger.warning("Transcript %s did not appear before deadline", self._output_path)
                self._event_queue.put_nowait(None)
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(sender_task, timeout=1.0)
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender_task
                return False
            await asyncio.sleep(POLL_INTERVAL)

    async def _sender_loop(self) -> None:
        """Drain the event queue, invoking on_event per event.

        Isolated from the tail so Telegram flood waits inside on_event
        (asyncio.sleep(retry_after)) can never block file reads. Exceptions
        never propagate — a broken send must not kill the stream.
        """
        session_name = self._session_name()
        while True:
            event = await self._event_queue.get()
            if event is None:  # sentinel — flush done
                logger.info("TUI_IO: sender_loop drain-exit session=%s", session_name)
                return
            # Per-event dispatch log: DEBUG — fires on every status/tool_use/
            # text frame, INFO would flood journalctl in a long CC run.
            logger.debug(
                "TUI_IO: sender_loop dispatch session=%s type=%s len=%d",
                session_name,
                event.type,
                len(event.content or ""),
            )
            started = time.monotonic()
            try:
                ret = self._on_event(event)
                if asyncio.iscoroutine(ret):
                    await ret
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "on_event failed on channel %s (event=%s)",
                    session_name,
                    event.type,
                    exc_info=True,
                )
            elapsed = time.monotonic() - started
            if elapsed > ON_EVENT_SLOW_WARN_SEC:
                logger.warning(
                    "Sender on_event blocked for %.1fs on channel %s (event=%s)",
                    elapsed,
                    session_name,
                    event.type,
                )

    def _read_new_lines(self) -> list[str]:
        """Blocking read of new lines from current offset."""
        lines: list[str] = []
        try:
            with self._output_path.open("r", errors="replace") as f:
                f.seek(self._offset())
                while True:
                    line = f.readline()
                    if not line:
                        break
                    self._set_offset(f.tell())
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
        except FileNotFoundError:
            pass
        return lines

    def _enqueue(self, event: StreamEvent) -> None:
        """Hand the event to the sender. Never blocks the tail."""
        self._event_queue.put_nowait(event)
        qsize = self._event_queue.qsize()
        if qsize > EVENT_QUEUE_BACKLOG_WARN and not self._backlog_warned:
            logger.warning(
                "Event queue backlog on %s: %d pending — sender stuck?",
                self._session_name(),
                qsize,
            )
            self._backlog_warned = True

    async def _process_lines(self, lines: list[str]) -> bool:
        """Process parsed JSON lines.

        For CC TUI transcripts this returns False — they don't emit
        terminal events. For codex, `task_complete` would normally
        return True (the tail closes); instead we open a grace window
        (`TASK_COMPLETE_GRACE_SEC`) so codex can keep streaming its
        final paragraph after declaring done. The grace window is
        evaluated by the caller in `run()` against
        `_task_complete_grace_deadline`.

        Any event arriving while the grace window is active resets the
        deadline — the tail only closes after a full silent grace
        period.
        """
        for line in lines:
            done = False
            if getattr(self._state, "provider", "claude") == "codex":
                parsed = CODEX_ADAPTER.parse_tui_event(line)
                events = parsed.events
                new_sid = parsed.session_id
                done = parsed.done
            else:
                events, new_sid = parse_transcript_event(line)
            if new_sid:
                # Observability only — state.session_id is owned by
                # start_session / switch_session / clear_context, never by
                # the tail. Leaking per-event sessionId into state risks
                # silent desync if a foreign id appears in the transcript.
                self._session_id = new_sid
                state_sid = self._state_session_id()
                if new_sid != state_sid and new_sid not in self._warned_rotated_sids:
                    logger.warning(
                        "TUI_IO: session_id rotation detected "
                        "channel=%s session=%s expected=%s observed=%s "
                        "offset=%d — state unchanged (observability only)",
                        self._channel_key,
                        self._session_name(),
                        state_sid,
                        new_sid,
                        self._offset(),
                    )
                    self._warned_rotated_sids.add(new_sid)

            for event in events:
                if event.type == "result":
                    # CC TUI transcript does not emit `result` events — this
                    # branch exists for legacy stream-json and is kept to match
                    # the pre-refactor behaviour exactly. `continue` (not
                    # `return True`) is intentional: the tail exits on cancel,
                    # tmux death, or idle, not on result.
                    if event.content:
                        self._enqueue(StreamEvent("result_message", event.content))
                    self._enqueue(StreamEvent("result", ""))
                    continue
                self._enqueue(event)

            # Reset the grace window on any event that arrives after
            # task_complete: codex is still streaming, give it more time.
            if events and self._task_complete_grace_deadline is not None:
                self._task_complete_grace_deadline = time.monotonic() + TASK_COMPLETE_GRACE_SEC

            if done:
                if getattr(self._state, "provider", "claude") == "codex":
                    if self._task_complete_grace_deadline is None:
                        self._task_complete_grace_deadline = (
                            time.monotonic() + TASK_COMPLETE_GRACE_SEC
                        )
                        logger.info(
                            "Tmux tail entered task_complete grace session=%s grace_sec=%.1f",
                            self._session_name(),
                            TASK_COMPLETE_GRACE_SEC,
                        )
                else:
                    return True
        return False

    # --- thin accessors on state (structural, see _StateAccess protocol) ---

    def _session_name(self) -> str:
        # Cached to avoid a Python attribute lookup per log line inside
        # the hot sender loop; session_name is immutable for this state's
        # lifetime.
        return self._session_name_cached

    def _state_session_id(self) -> str | None:
        return self._state.session_id

    def _offset(self) -> int:
        return self._state.offset

    def _set_offset(self, offset: int) -> None:
        self._state.offset = offset
