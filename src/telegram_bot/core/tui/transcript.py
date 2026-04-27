"""Transcript jsonl parsing + StreamEvent adapter for tmux-TUI mode.

`parse_jsonl_line` normalises one CC transcript line into a `ParsedEvent`.
`parse_transcript_event` is the adapter that turns a ParsedEvent into the
`StreamEvent` shape the bot's stream pipeline already knows how to render,
so tmux-TUI output reuses the same UI path as classic subprocess mode.

Coupling note: `parse_transcript_event` imports `_tool_status` from
`telegram_bot.core.services.claude` (and transitively relies on
`_smart_file_status`, `_smart_bash_status`, `_tool_status_map` inside it).
These are private (underscore-prefixed) but imported across the module
boundary intentionally so the tmux-TUI surface reuses the UX-consistent
status labels from subprocess mode. Any refactor of `_tool_status` or its
internal helpers in `claude.py` must update this module too.
TODO: consider promoting `_tool_status` to public `claude.tool_status`
in a later task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import StreamEvent, _tool_status

# `system` is intentionally NOT filtered wholesale — compact_boundary /
# status=compacting arrive as type=system,subtype=... and must reach the
# StreamEvent pipeline so the user sees compaction feedback in tmux mode.
# Unknown system subtypes collapse to skip via the explicit branch below.
FILTERED_TYPES = frozenset(
    {
        "permission-mode",
        "attachment",
        "file-history-snapshot",
    }
)

ParsedKind = Literal["text", "tool_use", "tool_result", "thinking", "status", "skip"]


@dataclass(frozen=True)
class ParsedEvent:
    """Normalised event surface for the bot's stream pipeline."""

    kind: ParsedKind
    payload: dict[str, Any]


def parse_jsonl_line(raw: str) -> ParsedEvent | None:
    """Parse one jsonl line. Return None only on malformed json.

    Filtered/system events become `ParsedEvent(kind="skip", ...)` with a
    reason hint so callers can warn-once on unknown types without
    reprocessing them.
    """
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError:
        return None

    etype = evt.get("type")
    if etype in FILTERED_TYPES:
        return ParsedEvent(kind="skip", payload={"reason": etype})

    if etype == "system":
        # CC 2.1.116 writes compact lifecycle as type=system. Surface the
        # user-facing status events here; everything else (file-history,
        # ad-hoc diagnostics) collapses to skip so TmuxManager's warn-once
        # logic sees the reason and stays quiet.
        subtype = evt.get("subtype")
        if subtype == "status" and evt.get("status") == "compacting":
            return ParsedEvent(kind="status", payload={"text": t("ui.compacting")})
        if subtype == "compact_boundary":
            meta = evt.get("compactMetadata") or evt.get("compact_metadata") or {}
            pre = int(meta.get("preTokens", meta.get("pre_tokens", 0)))
            post = int(meta.get("postTokens", meta.get("post_tokens", 0)))
            return ParsedEvent(
                kind="status",
                payload={"text": t("ui.compact_done", pre=pre, post=post)},
            )
        return ParsedEvent(kind="skip", payload={"reason": f"system:{subtype or '?'}"})

    if etype == "user":
        content = evt.get("message", {}).get("content")
        if isinstance(content, str):
            return ParsedEvent(kind="skip", payload={"reason": "user_echo"})
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            if blocks:
                return ParsedEvent(kind="tool_result", payload={"blocks": blocks})
        return ParsedEvent(kind="skip", payload={"reason": "user_unknown"})

    if etype == "assistant":
        content = evt.get("message", {}).get("content", [])
        # Assistant messages can mix blocks (e.g. `[thinking, text]` on Opus with
        # extended thinking). Scan all blocks and prefer real output — text then
        # tool_use — over thinking, otherwise the text gets silently dropped.
        text_block: dict[str, Any] | None = None
        tool_block: dict[str, Any] | None = None
        thinking_block: dict[str, Any] | None = None
        for block in content:
            btype = block.get("type")
            if btype == "text" and text_block is None:
                text_block = block
            elif btype == "tool_use" and tool_block is None:
                tool_block = block
            elif btype == "thinking" and thinking_block is None:
                thinking_block = block
        if text_block is not None:
            return ParsedEvent(kind="text", payload={"text": text_block.get("text", "")})
        if tool_block is not None:
            return ParsedEvent(
                kind="tool_use",
                payload={
                    "name": tool_block.get("name"),
                    "input": tool_block.get("input", {}),
                    "id": tool_block.get("id"),
                },
            )
        if thinking_block is not None:
            return ParsedEvent(
                kind="thinking",
                payload={"text": thinking_block.get("thinking", "")},
            )
        return ParsedEvent(kind="skip", payload={"reason": "assistant_empty"})

    return ParsedEvent(kind="skip", payload={"reason": f"unknown:{etype}"})


def tail_transcript(path: Path) -> list[ParsedEvent]:
    """One-shot read of a transcript file → list of ParsedEvent.

    Used in tests for deterministic assertions. The production tail is
    incremental (offset-based), but reuses `parse_jsonl_line` per line.
    """
    if not path.exists():
        return []
    out: list[ParsedEvent] = []
    for raw in path.read_text().splitlines():
        parsed = parse_jsonl_line(raw)
        if parsed is not None:
            out.append(parsed)
    return out


def parse_transcript_event(
    raw: str,
) -> tuple[list[StreamEvent], str | None]:
    """Adapt one transcript jsonl line to the bot's StreamEvent pipeline.

    Returns `(events, session_id)` mirroring `claude.parse_cc_event`'s shape.
    `thinking`, `tool_result`, `skip`, and unknown types collapse to `([], None)`
    — the caller (TmuxManager) owns warn-once bookkeeping for unknown types.
    """
    parsed = parse_jsonl_line(raw)
    if parsed is None:
        return [], None

    # session_id is only surfaced when the raw event carries an explicit field.
    # Not observed in PoC on CC 2.1.114 but the defensive read is cheap.
    session_id: str | None = None
    try:
        evt = json.loads(raw)
        sid = evt.get("sessionId")
        if isinstance(sid, str):
            session_id = sid
    except json.JSONDecodeError:
        pass

    if parsed.kind in ("skip", "thinking", "tool_result"):
        return [], session_id

    if parsed.kind == "text":
        text = parsed.payload.get("text", "")
        if not text:
            return [], session_id
        return [StreamEvent("text", text)], session_id

    if parsed.kind == "tool_use":
        name = parsed.payload.get("name", "")
        tool_input = parsed.payload.get("input")
        status = _tool_status(name, tool_input if isinstance(tool_input, dict) else None)
        return [StreamEvent("status", status)], session_id

    if parsed.kind == "status":
        text = parsed.payload.get("text", "")
        if not text:
            return [], session_id
        return [StreamEvent("status", text)], session_id

    return [], session_id
