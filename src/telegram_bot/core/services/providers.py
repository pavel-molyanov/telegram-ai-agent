"""Provider adapters for Claude Code and OpenAI Codex CLI.

The bot has two independent axes:

* provider/engine: which agent CLI to run (Claude or Codex)
* exec_mode: how to run it (subprocess or tmux)

Keeping the provider-specific command and parser contracts here prevents
`exec_mode` from being overloaded with engine names.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from telegram_bot.core.services.cc_events import StreamEvent, _tool_status
from telegram_bot.core.services.codex_mcp import build_codex_mcp_config_args

logger = logging.getLogger(__name__)

Engine = Literal["claude", "codex"]
_CODEX_BOT_HOME = Path.home() / ".codex-bot"
_CODEX_HOME = Path.home() / ".codex"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in _TRUTHY_ENV_VALUES


def _use_codex_bot_home() -> bool:
    return (
        not _env_truthy("TELEGRAM_CODEX_SHARED_HOME") and (_CODEX_BOT_HOME / "config.toml").exists()
    )


def _ensure_codex_global_skill_links() -> None:
    """Expose global Codex skills inside the bot's isolated CODEX_HOME."""
    source_root = _CODEX_HOME / "skills"
    target_root = _CODEX_BOT_HOME / "skills"
    if not source_root.is_dir() or not target_root.is_dir():
        return

    for target in target_root.iterdir():
        if not target.is_symlink():
            continue
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            target.unlink()
            continue
        try:
            resolved.relative_to(source_root)
        except ValueError:
            continue
        if not (resolved / "SKILL.md").is_file():
            target.unlink()

    for source in source_root.iterdir():
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        target = target_root / source.name
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(source, target_is_directory=True)


def engine_display_name(engine: str) -> str:
    """Return a human-facing provider name for chat notifications."""
    if engine == "codex":
        return "Codex"
    if engine == "claude":
        return "Claude Code"
    return engine


def _codex_tui_prefix() -> list[str]:
    """Use the bot's lightweight Codex home when it is provisioned.

    Bot TUI sessions pass topic-scoped MCP servers through CLI overrides. A
    large or broken user-level ~/.codex/config.toml should not block fresh chat
    creation, so production provides ~/.codex-bot with shared auth and minimal
    config. Operators can temporarily opt into the regular home with
    ``TELEGRAM_CODEX_SHARED_HOME=1``.
    """
    if _use_codex_bot_home():
        _ensure_codex_global_skill_links()
        return ["env", f"CODEX_HOME={_CODEX_BOT_HOME}"]
    return []


def _codex_sessions_root(home: Path | None = None) -> Path:
    if home is not None:
        return home / ".codex" / "sessions"
    if _use_codex_bot_home():
        return _CODEX_BOT_HOME / "sessions"
    return Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class ExecCommand:
    argv: list[str]
    cwd: str
    stdin_text: str | None = None
    output_last_message_path: Path | None = None


@dataclass(frozen=True)
class ExecParseResult:
    events: list[StreamEvent]
    session_id: str | None = None


@dataclass(frozen=True)
class TuiParseResult:
    events: list[StreamEvent]
    session_id: str | None = None
    done: bool = False


@dataclass(frozen=True)
class TuiSessionInfo:
    session_id: str
    transcript_path: Path
    tail_start_offset: int = 0


@dataclass(frozen=True)
class TranscriptFileState:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class CodexTranscriptSnapshot:
    root: Path
    files: dict[Path, TranscriptFileState]


class ProviderAdapter(Protocol):
    name: Engine

    def parse_exec_event(self, raw: str) -> ExecParseResult: ...

    def build_tui_start(
        self, *, cwd: str, model: str | None = None, mcp_config: str | None = None
    ) -> list[str]: ...

    def build_tui_resume(
        self,
        *,
        cwd: str,
        session_id: str,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> list[str]: ...

    def parse_tui_event(self, raw: str) -> TuiParseResult: ...

    def is_prompt_ready(self, pane: str) -> bool: ...

    def is_modal_present(self, pane: str) -> bool: ...

    def transcript_path_for_state(
        self, *, cwd: str, session_id: str, transcript_path: str | None
    ) -> Path | None: ...


def _load_json(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class CodexAdapter:
    name: Engine = "codex"
    _MODAL_TAIL_LINES = 20

    @staticmethod
    def _is_subagent_source(source: object) -> bool:
        if isinstance(source, dict):
            return "subagent" in source
        if isinstance(source, str):
            return source == "subagent"
        return False

    def _meta_session_id(self, payload: dict[str, Any], *, cwd: str) -> str | None:
        if payload.get("originator") != "codex-tui" or not self._cwd_matches(
            payload.get("cwd"), cwd
        ):
            return None
        if self._is_subagent_source(payload.get("source")):
            return None
        session_id = payload.get("id")
        return session_id if isinstance(session_id, str) and session_id else None

    @staticmethod
    def _cwd_matches(value: object, cwd: str) -> bool:
        if not isinstance(value, str):
            return False
        if value == cwd:
            return True
        try:
            return Path(value).resolve() == Path(cwd).resolve()
        except OSError:
            return False

    def capture_tui_transcript_snapshot(
        self, *, home: Path | None = None
    ) -> CodexTranscriptSnapshot:
        root = _codex_sessions_root(home)
        files: dict[Path, TranscriptFileState] = {}
        for path in root.glob("**/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files[path.resolve()] = TranscriptFileState(
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        return CodexTranscriptSnapshot(root=root, files=files)

    @staticmethod
    def _iter_complete_json_lines_from(path: Path, offset: int) -> list[dict[str, Any]]:
        try:
            with path.open("rb") as f:
                f.seek(offset)
                raw = f.read()
        except OSError:
            return []
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            parts = raw.rsplit(b"\n", 1)
            raw = b"" if len(parts) == 1 else parts[0] + b"\n"
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            data = _load_json(line.decode("utf-8", errors="replace"))
            if data is not None:
                records.append(data)
        return records

    def _session_id_from_meta(self, path: Path, *, cwd: str, max_records: int = 5) -> str | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if idx >= max_records:
                        break
                    data = _load_json(line)
                    if not data or data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload")
                    if isinstance(payload, dict):
                        return self._meta_session_id(payload, cwd=cwd)
        except OSError:
            return None
        return None

    @staticmethod
    def _normalize_prompt_for_match(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return value.rstrip("\r\n")

    @classmethod
    def _prompts_match(cls, value: object, prompt: str) -> bool:
        return cls._normalize_prompt_for_match(value) == cls._normalize_prompt_for_match(prompt)

    @classmethod
    def _has_prompt_user_message(cls, records: list[dict[str, Any]], prompt: str) -> int:
        matches = 0
        for data in records:
            if data.get("type") != "event_msg":
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "user_message":
                continue
            if cls._prompts_match(payload.get("message"), prompt):
                matches += 1
        return matches

    @staticmethod
    def _command_from_exec_payload(payload: dict[str, Any]) -> str | None:
        command = payload.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            parts = [str(part) for part in command]
            if len(parts) >= 3 and parts[0].endswith("bash") and parts[1] == "-lc":
                return parts[2]
            return " ".join(parts)
        if isinstance(command, str) and command:
            return command

        parsed_cmd = payload.get("parsed_cmd")
        if isinstance(parsed_cmd, list):
            for item in parsed_cmd:
                if isinstance(item, dict):
                    cmd = item.get("cmd")
                    if isinstance(cmd, str) and cmd:
                        return cmd
        return None

    @staticmethod
    def _message_text_from_payload(payload: dict[str, Any]) -> str | None:
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
        text = payload.get("text")
        if isinstance(text, str) and text:
            return text
        content = payload.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_text = item.get("text")
                if isinstance(item_text, str) and item_text:
                    parts.append(item_text)
            if parts:
                return "".join(parts)
        return None

    @staticmethod
    def _status_for_codex_function_call(name: str, tool_input: dict[str, object] | None) -> str:
        if name.startswith("functions."):
            name = name.split(".", 1)[1]
        if name == "exec_command" and isinstance(tool_input, dict):
            cmd = tool_input.get("cmd")
            if isinstance(cmd, str) and cmd:
                return _tool_status("Bash", {"command": cmd})
        return _tool_status(name, tool_input)

    def binary(self) -> str:
        """Return an executable Codex CLI path that works in service processes.

        The bot service may not inherit the interactive shell PATH, while
        npm-global commonly installs Codex under ~/.npm-global/bin. Using the
        absolute path prevents tmux from opening and immediately closing with
        "codex: command not found", which otherwise looks like a TUI readiness
        timeout.
        """
        fallback = Path.home() / ".npm-global" / "bin" / "codex"
        if self._is_safe_binary(fallback):
            return str(fallback)
        if found := shutil.which("codex"):
            candidate = Path(found)
            if candidate.is_absolute() and self._is_safe_binary(candidate):
                return str(candidate)
        # Return the explicit expected path so process spawn fails loudly
        # instead of searching a service PATH that may not contain Codex.
        return str(fallback)

    @staticmethod
    def _is_safe_binary(path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        return stat.st_uid == os.getuid() and stat.st_mode & 0o022 == 0

    def parse_exec_event(self, raw: str) -> ExecParseResult:
        data = _load_json(raw)
        if data is None:
            return ExecParseResult([])

        event_type = data.get("type")
        if event_type == "thread.started":
            thread_id = data.get("thread_id")
            return ExecParseResult([], thread_id if isinstance(thread_id, str) else None)

        payload = data.get("payload")
        if event_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "function_call":
                name = payload.get("name", "")
                args = payload.get("arguments")
                tool_input: dict[str, object] | None = None
                if isinstance(args, str):
                    parsed_args = _load_json(args)
                    tool_input = parsed_args if parsed_args is not None else None
                elif isinstance(args, dict):
                    tool_input = args
                status = self._status_for_codex_function_call(str(name), tool_input)
                return ExecParseResult([StreamEvent("status", status)])
            if payload_type == "tool_search_call":
                return ExecParseResult([StreamEvent("status", "Ищу инструмент...")])
            if payload_type == "message" and payload.get("role") == "assistant":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])

        if event_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])
            if payload_type == "exec_command_end":
                exit_code = payload.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    return ExecParseResult([])
                command = self._command_from_exec_payload(payload)
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                return ExecParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])

        item = data.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "command_execution":
                command = item.get("command")
                exit_code = item.get("exit_code")
                if event_type == "item.completed" and (
                    not isinstance(exit_code, int) or exit_code == 0
                ):
                    return ExecParseResult([])
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                if event_type == "item.completed" and isinstance(exit_code, int) and exit_code:
                    status = f"{status} (exit {exit_code})"
                return ExecParseResult([StreamEvent("status", status)])
            if event_type == "item.completed" and item_type == "agent_message":
                # Final answer is read from --output-last-message after exit.
                return ExecParseResult([])

        return ExecParseResult([])

    def build_tui_start(
        self, *, cwd: str, model: str | None = None, mcp_config: str | None = None
    ) -> list[str]:
        cmd = [
            *_codex_tui_prefix(),
            self.binary(),
            *build_codex_mcp_config_args(mcp_config, ignore_user_config=False),
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            cwd,
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd

    def build_tui_resume(
        self,
        *,
        cwd: str,
        session_id: str,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> list[str]:
        cmd = [
            *_codex_tui_prefix(),
            self.binary(),
            "resume",
            *build_codex_mcp_config_args(mcp_config, ignore_user_config=False),
            session_id,
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            cwd,
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd

    def parse_tui_event(self, raw: str) -> TuiParseResult:
        data = _load_json(raw)
        if data is None:
            return TuiParseResult([])

        event_type = data.get("type")
        payload = data.get("payload")
        if event_type == "session_meta" and isinstance(payload, dict):
            session_id = payload.get("id")
            return TuiParseResult(
                [],
                session_id=session_id if isinstance(session_id, str) else None,
            )

        if event_type == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type")
            if ptype == "agent_message":
                message = payload.get("message")
                if not isinstance(message, str) or not message:
                    return TuiParseResult([])
                if payload.get("phase") == "final_answer":
                    return TuiParseResult([StreamEvent("result_message", message)])
                return TuiParseResult([StreamEvent("text", message)])
            if ptype == "exec_command_end":
                exit_code = payload.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    return TuiParseResult([])

                command = self._command_from_exec_payload(payload)
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                return TuiParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])
            if ptype == "task_complete":
                return TuiParseResult([StreamEvent("result", "")], done=True)

        if (
            event_type == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "function_call"
        ):
            name = payload.get("name", "")
            args = payload.get("arguments")
            tool_input: dict[str, object] | None = None
            if isinstance(args, str):
                parsed_args = _load_json(args)
                tool_input = parsed_args if parsed_args is not None else None
            elif isinstance(args, dict):
                tool_input = args
            status = self._status_for_codex_function_call(str(name), tool_input)
            return TuiParseResult([StreamEvent("status", status)])

        # Assistant response_item messages are intentionally ignored:
        # Codex also emits event_msg agent_message for commentary/final answers,
        # and that path is the single delivery source to avoid duplicates.

        return TuiParseResult([])

    def is_prompt_ready(self, pane: str) -> bool:
        return "\u203a" in pane

    def is_modal_present(self, pane: str) -> bool:
        # Codex leaves prior dialogs in scrollback after they are dismissed.
        # Trim physical blank padding and inspect only the live tail, otherwise
        # old "trust this directory" or normal output mentioning settings.json
        # produces repeated false modal alerts while the agent is simply working.
        lines = pane.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        tail = "\n".join(lines[-self._MODAL_TAIL_LINES :]).lower()
        markers = (
            "allow command",
            "approval required",
            "do you trust the contents of this directory",
            "select model and effort",
            "press enter to confirm",
            "press enter to continue",
            "press enter to select",
            "enter to confirm",
            "esc to cancel",
            "esc to dismiss",
            "no, quit",
            # Codex Question dialog (multi-choice with optional notes).
            # Footer: `tab to add notes | enter to submit answer | esc to interrupt`.
            # Without these markers the bot saw codex's interactive picker as
            # plain idle pane and never surfaced the TUI keyboard, so the user
            # could not answer the question from Telegram (reported 2026-04-26).
            "enter to submit answer",
            "tab to add notes",
        )
        return any(marker in tail for marker in markers)

    def transcript_path_for_state(
        self, *, cwd: str, session_id: str, transcript_path: str | None
    ) -> Path | None:
        if transcript_path:
            path = Path(transcript_path)
            return path if path.exists() else None
        return self.find_tui_transcript(cwd=cwd, session_id=session_id)

    def find_tui_transcript(
        self, *, cwd: str, session_id: str, home: Path | None = None
    ) -> Path | None:
        """Find one existing Codex TUI transcript for ``session_id`` and ``cwd``.

        Collisions fail closed: returning None is safer than tailing an
        arbitrary transcript from a different run.
        """
        root = _codex_sessions_root(home)
        matches: list[Path] = []
        for path in root.glob(f"**/*{session_id}*.jsonl"):
            try:
                first = path.read_text(errors="replace").splitlines()[0]
                data = _load_json(first)
            except (OSError, IndexError):
                continue
            if not data or data.get("type") != "session_meta":
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                continue
            if self._meta_session_id(payload, cwd=cwd) == session_id:
                matches.append(path.resolve())
        if len(matches) == 1:
            return matches[0]
        return None

    async def locate_tui_transcript(
        self,
        *,
        cwd: str,
        snapshot: CodexTranscriptSnapshot,
        prompt: str,
        home: Path | None = None,
        timeout_sec: float = 30.0,
    ) -> TuiSessionInfo:
        root = _codex_sessions_root(home) if home is not None else snapshot.root
        deadline = time.monotonic() + timeout_sec
        settle_sec = min(0.25, timeout_sec)
        single_since: float | None = None
        single_identity: tuple[str, Path, int] | None = None
        last_matches: list[tuple[str, Path, int]] = []
        while True:
            candidates: list[TuiSessionInfo] = []
            candidate_meta: list[tuple[str, Path, int]] = []
            for path in root.glob("**/*.jsonl"):
                resolved = path.resolve()
                try:
                    stat = path.stat()
                except OSError:
                    continue
                previous = snapshot.files.get(resolved)
                if previous is not None and stat.st_size <= previous.size:
                    continue
                baseline = (
                    previous.size if previous is not None and stat.st_size >= previous.size else 0
                )
                session_id = self._session_id_from_meta(path, cwd=cwd)
                if not session_id:
                    continue
                records = self._iter_complete_json_lines_from(path, baseline)
                prompt_matches = self._has_prompt_user_message(records, prompt)
                if prompt_matches == 0:
                    continue
                for _ in range(prompt_matches):
                    candidates.append(TuiSessionInfo(session_id, resolved, baseline))
                    candidate_meta.append(
                        (
                            session_id,
                            resolved,
                            baseline,
                        )
                    )
            last_matches = candidate_meta
            if len(candidates) > 1:
                logger.warning(
                    "Codex TUI transcript prompt collision for cwd=%s candidates=%s",
                    cwd,
                    candidate_meta,
                )
                raise RuntimeError("Codex TUI transcript collision")
            if len(candidates) == 1:
                identity = (
                    candidates[0].session_id,
                    candidates[0].transcript_path,
                    candidates[0].tail_start_offset,
                )
                now = time.monotonic()
                if single_identity != identity:
                    single_identity = identity
                    single_since = now
                if single_since is not None and (
                    now - single_since >= settle_sec or now >= deadline
                ):
                    return candidates[0]
            else:
                single_identity = None
                single_since = None
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.2, max(deadline - time.monotonic(), 0.0)))
        logger.warning(
            "Codex TUI transcript not found for cwd=%s root=%s snapshot_files=%d matches=%s",
            cwd,
            root,
            len(snapshot.files),
            last_matches,
        )
        raise TimeoutError("Codex TUI transcript not found")


CODEX_ADAPTER = CodexAdapter()


def is_engine_available(engine: str) -> bool:
    """Return whether the provider CLI can be spawned by the current process."""
    if engine == "claude":
        return shutil.which("claude") is not None
    if engine == "codex":
        return CODEX_ADAPTER._is_safe_binary(Path(CODEX_ADAPTER.binary()))
    return False


def choose_available_engine(preferred: str = "claude") -> Engine | None:
    """Pick an installed engine, preferring the requested one and then the other."""
    if preferred in {"claude", "codex"} and is_engine_available(preferred):
        return preferred  # type: ignore[return-value]
    fallback: Engine = "codex" if preferred == "claude" else "claude"
    if is_engine_available(fallback):
        return fallback
    return None
