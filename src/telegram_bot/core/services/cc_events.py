"""Claude Code event parsing and tool-status labels.

Extracted from `claude.py`. Holds:

- Tool-status label registry (`_tool_status_map`, `_smart_file_status`,
  `_smart_bash_status`, `_tool_status`).
- `parse_cc_event` — stream-json event → `StreamEvent` list.
- `StreamEvent` dataclass (returned by the parser).
- Module-level `_EXTRA_*` registries that the private entry point
  populates at startup via `SessionManager.extend_*`. Keeping them here
  (next to `_tool_status_map` / `_bash_cmd_rules` / `_file_path_rules`)
  avoids threading dict references through the codebase.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telegram_bot.core.messages import t

# Module-level `_EXTRA_*` registries let downstream entry points inject extra
# local rules without shipping those strings in the public core. Populate via
# the corresponding SessionManager.extend_* methods at bot startup.

_EXTRA_TOOL_STATUS: dict[str, str] = {}
_EXTRA_FILE_PATH_RULES: list[tuple[str, str, str]] = []
_EXTRA_BASH_RULES: list[tuple[str, str]] = []


def _tool_status_map() -> dict[str, str]:
    """Build tool → status label map. Called per-use to honor bot_lang."""
    mapping: dict[str, str] = {
        "Read": t("tool.read"),
        "Grep": t("tool.grep"),
        "Glob": t("tool.glob"),
        "Bash": t("tool.bash"),
        "Write": t("tool.write"),
        "Edit": t("tool.edit"),
        "Skill": t("tool.skill"),
        "Agent": t("tool.agent"),
        "mcp__bot__send_message": t("tool.send_message"),
        "mcp__bot__send_image": t("tool.send_image"),
        "mcp__bot__send_document": t("tool.send_document"),
    }
    mapping.update(_EXTRA_TOOL_STATUS)
    return mapping


# Backwards-compat alias — some tests/modules import this name. Evaluated lazily.
TOOL_STATUS_MAP = _tool_status_map()


def _mcp_fallback(tool_name: str) -> str:
    """Friendly label for any unknown `mcp__<server>__<method>` tool.

    `mcp__foo__listBar` → `🔌 foo...`. Keeps the public core free of any
    specific MCP-server name while still giving the user a readable hint.
    """
    parts = tool_name.split("__")
    server = parts[1] if len(parts) > 1 and parts[1] else "mcp"
    return f"🔌 {server}..."


# --- Smart file path detection for Read/Write/Edit ---


def _file_path_rules() -> list[tuple[str, str, str]]:
    """Build (path_pattern, read_status, write_status) rules."""
    mem_r = t("tool.read_memory")
    mem_w = t("tool.write_memory")
    skill_r = t("tool.read_skill")
    skill_w = t("tool.write_skill")
    return [
        ("memory/", mem_r, mem_w),
        ("MEMORY.md", mem_r, mem_w),
        (".claude/skills/", skill_r, skill_w),
        *_EXTRA_FILE_PATH_RULES,
    ]


def _smart_file_status(tool_name: str, file_path: str) -> str:
    """Return context-aware status for file operations based on path patterns."""
    is_read = tool_name == "Read"
    for pattern, read_status, write_status in _file_path_rules():
        if pattern in file_path:
            base = read_status if is_read else write_status
            return f"{base}: {Path(file_path).name}"
    # Default: generic status with basename
    base = _tool_status_map()[tool_name]
    return f"{base} {Path(file_path).name}"


# --- Smart Bash command detection ---


def _bash_cmd_rules() -> list[tuple[str, str]]:
    """Build (pattern_substring, status) rules for Bash command detection.

    Downstream rules are attached at runtime via
    SessionManager.extend_bash_rules; core keeps only generic tools
    (git/pytest/curl/wget/node).
    """
    return [
        *_EXTRA_BASH_RULES,
        ("git commit", "📦 Git: commit"),
        ("git push", "📦 Git: push"),
        ("git pull", "📦 Git: pull"),
        ("git add", "📦 Git: add"),
        ("git diff", "📦 Git: diff"),
        ("git log", "📦 Git: log"),
        ("git status", "📦 Git: status"),
        ("pytest", t("tool.run_tests")),
        ("python -m pytest", t("tool.run_tests")),
        ("curl ", t("tool.fetch_url")),
        ("wget ", t("tool.fetch_url")),
        ("node -e", t("tool.calc_time")),
    ]


def _bash_prefix_rules() -> list[tuple[str, str]]:
    """Build (command_prefix, status) rules — matched by prefix (e.g. "date" ./ "date +%Y")."""
    return [
        ("date", t("tool.check_time")),
    ]


def _redact_shell_command(command: str) -> str:
    """Return a Telegram-safe one-line command summary."""
    redacted = " ".join(command.split())
    redacted = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", redacted)
    redacted = re.sub(
        r"(?i)([?&][^=\s&]*(?:token|secret|password|passwd|api[_-]?key|auth|key)[^=\s&]*=)"
        r"[^&\s]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(^|\s)([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|PAT|PRIVATE|"
        r"CREDENTIAL|AUTH|COOKIE|SESSION)[A-Z0-9_]*=)(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(--?(?:token|password|passwd|secret|api-key|apikey|key|cookie|authorization|"
        r"private-key|access-key|pat|credential)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)((?:authorization|x-api-key|api-key):\s*(?:bearer\s+)?)\S+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", redacted)
    return redacted


def _smart_bash_status(command: str) -> str:
    """Return context-aware status for Bash commands."""
    for pattern, status in _bash_cmd_rules():
        if pattern in command:
            return status
    # Strip env vars like "TZ=Asia/Jakarta date ..." to get actual command
    bare = command.lstrip()
    while bare and "=" in bare.split()[0]:
        bare = bare.split(None, 1)[1] if " " in bare else ""
    for prefix, status in _bash_prefix_rules():
        if bare == prefix or bare.startswith(prefix + " ") or bare.startswith(prefix + "+"):
            return status
    return t("tool.bash_with_cmd", cmd=_redact_shell_command(command)[:60])


# tool_name → (input_key, format): basename, quoted(«»), truncate(:60), colon, raw
_TOOL_DETAIL_EXTRACTORS: dict[str, tuple[str, str]] = {
    "Read": ("file_path", "smart_file"),
    "Write": ("file_path", "smart_file"),
    "Edit": ("file_path", "smart_file"),
    "Grep": ("pattern", "quoted"),
    "Glob": ("pattern", "raw"),
    "Bash": ("command", "smart_bash"),
    "Skill": ("skill", "colon"),
    "Agent": ("description", "colon"),
}


def _tool_status(tool_name: str, tool_input: dict[str, object] | None = None) -> str:
    """Get human-readable status for a tool call with optional detail from input."""
    tool_map = _tool_status_map()
    if tool_name in tool_map:
        base = tool_map[tool_name]
    elif tool_name.startswith("mcp__bot__"):
        return f"🤖 {tool_name.split('__')[-1]}..."
    elif tool_name.startswith("mcp__"):
        return _mcp_fallback(tool_name)
    else:
        return f"⏳ {tool_name}..."

    if not tool_input or not isinstance(tool_input, dict):
        return base

    extractor = _TOOL_DETAIL_EXTRACTORS.get(tool_name)
    if not extractor:
        return base

    key, fmt = extractor
    try:
        value = tool_input[key]
        if not isinstance(value, str) or not value:
            return base
        if fmt == "smart_file":
            return _smart_file_status(tool_name, value)
        if fmt == "smart_bash":
            return _smart_bash_status(value)
        if fmt == "quoted":
            return f"{base} «{value[:80]}»"
        if fmt == "colon":
            return f"{base}: {value[:80]}"
        return f"{base} {value[:80]}"
    except (KeyError, TypeError):
        return base


def _agent_done_status(description: str) -> str:
    """Build status string for Agent tool completion."""
    if description:
        return t("tool.agent_done_with_desc", desc=description[:80])
    return t("tool.agent_done")


@dataclass
class StreamEvent:
    """One event from CC stream."""

    type: Literal["status", "text", "result", "result_message"]
    content: str
    session_id: str | None = None


def parse_cc_event(
    data: dict[str, Any],
    active_agents: dict[str, str],
    agent_last_progress: dict[str, float],
    throttle_sec: float,
) -> tuple[list[StreamEvent], str | None]:
    """Parse one CC stream-json event into StreamEvents.

    Returns (events, session_id_if_result_event).
    Modifies active_agents and agent_last_progress in place for agent
    lifecycle tracking.
    """
    events: list[StreamEvent] = []
    session_id: str | None = None

    event_type = data.get("type")

    if event_type == "system":
        subtype = data.get("subtype")

        if subtype == "status":
            if data.get("status") == "compacting":
                events.append(StreamEvent("status", t("ui.compacting")))

        elif subtype == "compact_boundary":
            # CC 2.1.116 jsonl writes camelCase (compactMetadata.preTokens/postTokens).
            # Older stream-json emitted snake_case. Accept either so both TUI
            # transcript replay and subprocess stream-json paths land here.
            meta = data.get("compactMetadata") or data.get("compact_metadata") or {}
            pre = int(meta.get("preTokens", meta.get("pre_tokens", 0)))
            post = int(meta.get("postTokens", meta.get("post_tokens", 0)))
            events.append(StreamEvent("status", t("ui.compact_done", pre=pre, post=post)))

        elif subtype == "task_started":
            # Track all agent types: local_agent (Agent tool) and team agents.
            tool_id = str(data.get("tool_use_id", ""))[:128]
            desc = str(data.get("description", ""))[:120]
            if tool_id and desc and len(active_agents) < 100:
                active_agents[tool_id] = desc

        elif subtype == "task_progress":
            tool_id = data.get("tool_use_id", "")
            desc = data.get("description", "")
            agent_name = active_agents.get(tool_id, "")
            if agent_name and desc and desc != agent_name:
                now = time.monotonic()
                last = agent_last_progress.get(tool_id, float("-inf"))
                if now - last >= throttle_sec:
                    short_name = agent_name[:25]
                    short_desc = desc[:60]
                    events.append(StreamEvent("status", f"🤖 {short_name} → {short_desc}"))
                    agent_last_progress[tool_id] = now

        elif subtype == "task_notification" and data.get("status") == "completed":
            tool_id = data.get("tool_use_id", "")
            desc = active_agents.pop(tool_id, "")
            agent_last_progress.pop(tool_id, None)
            if desc:
                events.append(StreamEvent("status", _agent_done_status(desc)))

    elif event_type == "assistant":
        for block in data.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input")
                events.append(StreamEvent("status", _tool_status(tool_name, tool_input)))
            elif block_type == "text":
                text = block.get("text", "")
                if text:
                    events.append(StreamEvent("text", text))

    elif event_type == "result":
        result_text = data.get("result", "") or ""
        session_id = data.get("session_id")
        events.append(StreamEvent("result", result_text, session_id))

    return events, session_id
