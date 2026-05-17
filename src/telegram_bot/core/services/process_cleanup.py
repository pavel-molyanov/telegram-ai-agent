"""Process cleanup and diagnostics for bot-owned agent runtimes."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

_TERM_GRACE_SEC = 1.0
_PROC = Path("/proc")


@dataclass(frozen=True)
class RuntimeProcess:
    pid: int
    ppid: int
    pgid: int
    sid: int
    rss_kb: int
    command: str
    args: str


@dataclass(frozen=True)
class RuntimeDiagnostics:
    pane_pid: int | None
    pane_sid: int | None
    sid_processes: tuple[RuntimeProcess, ...]
    tagged_processes: tuple[RuntimeProcess, ...]
    configured_servers: tuple[str, ...]

    @property
    def duplicate_generations(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        seen: set[int] = set()
        for proc in (*self.sid_processes, *self.tagged_processes):
            if proc.pid in seen:
                continue
            seen.add(proc.pid)
            kind = classify_mcp_process(proc.args)
            if kind:
                counts[kind] += 1
        return dict(counts)

    @property
    def rss_kb(self) -> int:
        seen: set[int] = set()
        total = 0
        for proc in (*self.sid_processes, *self.tagged_processes):
            if proc.pid in seen:
                continue
            seen.add(proc.pid)
            total += proc.rss_kb
        return total


def classify_mcp_process(args: str) -> str | None:
    if "mcp-servers/bot/server.py" in args:
        return "bot"
    if "telegram-mcp" in args:
        return "telegram"
    if "@playwright/mcp" in args:
        return "playwright"
    if "singularity" in args and "mcp" in args:
        return "singularity"
    if "google-docs-mcp" in args:
        return "google-docs"
    if "shadcn" in args and "mcp" in args:
        return "shadcn"
    return None


def tmux_pane_pid(session_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"={session_name}:", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = (getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _read_stat(pid: int) -> tuple[int, int, int, str] | None:
    try:
        raw = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = raw.rfind(")")
    if end == -1:
        return None
    command = raw[raw.find("(") + 1 : end]
    rest = raw[end + 2 :].split()
    try:
        ppid = int(rest[1])
        pgid = int(rest[2])
        sid = int(rest[3])
    except (IndexError, ValueError):
        return None
    return ppid, pgid, sid, command


def _read_rss_kb(pid: int) -> int:
    try:
        for line in (_PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1]) if len(parts) >= 2 else 0
    except OSError:
        return 0
    return 0


def _read_cmdline(pid: int) -> str:
    try:
        raw = (_PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _read_environ(pid: int) -> dict[str, str]:
    try:
        raw = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")
    return env


def _iter_pids() -> list[int]:
    pids: list[int] = []
    for entry in _PROC.iterdir():
        if entry.name.isdigit():
            pids.append(int(entry.name))
    return pids


def _process(pid: int) -> RuntimeProcess | None:
    stat = _read_stat(pid)
    if stat is None:
        return None
    ppid, pgid, sid, command = stat
    return RuntimeProcess(
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        sid=sid,
        rss_kb=_read_rss_kb(pid),
        command=command,
        args=_read_cmdline(pid),
    )


def processes_by_sid(sid: int) -> tuple[RuntimeProcess, ...]:
    result: list[RuntimeProcess] = []
    current = os.getpid()
    for pid in _iter_pids():
        if pid == current:
            continue
        proc = _process(pid)
        if proc is not None and proc.sid == sid:
            result.append(proc)
    return tuple(result)


def tagged_processes(
    *,
    channel_key: ChannelKey | None = None,
    tmux_session: str | None = None,
    runtime_path: str | None = None,
) -> tuple[RuntimeProcess, ...]:
    channel_value = f"{channel_key[0]}:{channel_key[1]}" if channel_key is not None else None
    result: list[RuntimeProcess] = []
    for pid in _iter_pids():
        env = _read_environ(pid)
        if not env:
            continue
        if channel_value and env.get("AI_ASSISTANT_CHANNEL_KEY") != channel_value:
            continue
        if tmux_session and env.get("AI_ASSISTANT_TMUX_SESSION") != tmux_session:
            continue
        if runtime_path and env.get("AI_ASSISTANT_MCP_RUNTIME") != runtime_path:
            continue
        proc = _process(pid)
        if proc is not None:
            result.append(proc)
    return tuple(result)


def terminate_processes(processes: tuple[RuntimeProcess, ...]) -> int:
    targets = {proc.pid for proc in processes if proc.pid != os.getpid()}
    if not targets:
        return 0

    for pid in sorted(targets):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("No permission to terminate pid=%s", pid)
    deadline = time.monotonic() + _TERM_GRACE_SEC
    while time.monotonic() < deadline:
        alive = [pid for pid in targets if (_PROC / str(pid)).exists()]
        if not alive:
            return len(targets)
        time.sleep(0.05)
    for pid in sorted(targets):
        if not (_PROC / str(pid)).exists():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("No permission to kill pid=%s", pid)
    return len(targets)


def cleanup_tmux_runtime(
    *,
    session_name: str,
    channel_key: ChannelKey | None = None,
    runtime_path: str | None = None,
) -> int:
    """Stop a tmux TUI runtime and processes in the pane's Unix session.

    The SID sweep is intentional: commands and MCP servers launched from the
    TUI belong to that terminal session and must go away on `/kill`/`/recycle`.
    Unrelated tmux sessions use different SIDs and are not targeted.
    """
    pane_pid = tmux_pane_pid(session_name)
    pane_sid: int | None = None
    if pane_pid is not None and (pane := _process(pane_pid)) is not None:
        pane_sid = pane.sid

    subprocess.run(["tmux", "kill-session", "-t", f"={session_name}"], capture_output=True)

    targets: dict[int, RuntimeProcess] = {}
    if pane_sid is not None:
        for proc in processes_by_sid(pane_sid):
            targets[proc.pid] = proc
    for proc in tagged_processes(
        channel_key=channel_key,
        tmux_session=session_name,
        runtime_path=runtime_path,
    ):
        targets[proc.pid] = proc

    killed = terminate_processes(tuple(targets.values()))
    if killed:
        logger.info("Cleaned %d runtime processes for tmux session %s", killed, session_name)
    return killed


def runtime_diagnostics(
    *,
    session_name: str,
    channel_key: ChannelKey,
    runtime_path: str | None,
    configured_servers: tuple[str, ...],
) -> RuntimeDiagnostics:
    pane_pid = tmux_pane_pid(session_name)
    pane_sid: int | None = None
    sid_processes: tuple[RuntimeProcess, ...] = ()
    if pane_pid is not None and (pane := _process(pane_pid)) is not None:
        pane_sid = pane.sid
        sid_processes = processes_by_sid(pane_sid)
    return RuntimeDiagnostics(
        pane_pid=pane_pid,
        pane_sid=pane_sid,
        sid_processes=sid_processes,
        tagged_processes=tagged_processes(
            channel_key=channel_key,
            tmux_session=session_name,
            runtime_path=runtime_path,
        ),
        configured_servers=configured_servers,
    )
