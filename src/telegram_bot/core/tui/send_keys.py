"""tmux input helpers for the Claude / Codex TUI.

Delivery uses **bracketed paste** (`tmux load-buffer` → `paste-buffer -p`).
The TUIs interpret bracketed-paste markers as a single user paste event
and collapse the body into a `[Pasted text]` / `[Pasted Content N chars]`
chip that submits on a single Enter — which is what the user actually
expects when sending a long message through Telegram.

The previous `tmux send-keys -l` path streamed bytes as typed input. On
text >800 chars or with embedded newlines the engines either fragmented
the input, ate the trailing Enter into their paste buffer, or required
extra Tab/Enter dances. That caused production message loss in topic 9
on 2026-04-26 — see tests/test_paste_buffer.py for the reproductions
that pin the new contract.

`plan_send_keys` and `SendKeysPlan` are kept as backward-compat re-exports
for integration tests that import them via tui_helpers.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# tmux's bracketed-paste end-marker. If a hostile or merely curious user
# pastes this byte sequence inside their prompt, the TUI would interpret
# it as "paste done" and treat the rest of the message as typed input —
# defeating the chip collapse. Strip it out before handing the payload
# to load-buffer. (start-marker is `\x1b[200~`; tmux generates that one,
# so the receiving end never reads it from the user payload.)
_BRACKETED_PASTE_END = "\x1b[201~"

# Legacy constants — kept so tests/integration/tui_helpers.py and the
# existing smoke tests that read them keep importing cleanly. The
# bracketed-paste path no longer applies char/newline thresholds.
PASTE_MODE_CHAR_THRESHOLD = 800
PASTE_MODE_NEWLINE_THRESHOLD = 2
PASTE_MODE_SETTLE_MS = 500


@dataclass(frozen=True)
class SendKeysPlan:
    """Backward-compat artefact of the send-keys -l era."""

    literal_text: str
    split_enter: bool
    settle_ms: int


def plan_send_keys(text: str) -> SendKeysPlan:
    """Backward-compat: returns the legacy plan shape used by older
    integration tests. New code goes through `send_paste`."""
    has_newlines = text.count("\n") > PASTE_MODE_NEWLINE_THRESHOLD
    long_text = len(text) > PASTE_MODE_CHAR_THRESHOLD
    needs_settle = has_newlines or long_text
    return SendKeysPlan(
        literal_text=text,
        split_enter=True,
        settle_ms=PASTE_MODE_SETTLE_MS if needs_settle else 0,
    )


async def send_paste(session_name: str, text: str) -> None:
    """Deliver `text` to `session_name` as one bracketed-paste event.

    Steps (all via async-thread subprocess.run):
      1. `tmux load-buffer -b <unique> -` — push the payload via stdin
         into a named tmux buffer. Stdin avoids ARG_MAX limits and any
         leading-hyphen parsing surprises.
      2. `tmux paste-buffer -p -b <unique> -t <session>` — paste with the
         bracketed-paste wrapper (`-p`). The TUI sees a single paste.
      3. `tmux delete-buffer -b <unique>` — drop the buffer. Tmux holds
         buffers per server; without delete they accumulate and a long-
         running bot starts leaking memory.

    Cleanup contract: if step 1 succeeds we always run step 3, even if
    step 2 fails or the surrounding task is cancelled. Errors from
    step 1 or 2 propagate as `CalledProcessError`; the caller
    (`TmuxManager._safe_send_*`) catches and surfaces a modal alert in
    Telegram. Step 3 is best-effort — its failures get logged but never
    mask the upstream error or raise on their own.

    The payload is sanitized: any embedded bracketed-paste end marker
    (`ESC[201~`) is removed so it cannot prematurely terminate the paste
    block from inside.
    """
    buffer_name = f"bot-paste-{uuid.uuid4().hex}"
    sanitized = text.replace(_BRACKETED_PASTE_END, "")
    loaded = False
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=sanitized.encode("utf-8"),
            check=True,
        )
        loaded = True
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "paste-buffer", "-p", "-b", buffer_name, "-t", session_name],
            check=True,
        )
    finally:
        # Only attempt cleanup if load-buffer succeeded — if it didn't,
        # there is no buffer to drop and the spurious "no buffer" error
        # would just clutter the log. capture_output=True keeps the
        # stderr noise out of the systemd journal in the happy path;
        # we surface it ourselves via logger.warning when delete fails.
        if loaded:
            cleanup = await asyncio.to_thread(
                subprocess.run,
                ["tmux", "delete-buffer", "-b", buffer_name],
                check=False,
                capture_output=True,
            )
            if cleanup.returncode != 0:
                logger.warning(
                    "tmux delete-buffer failed (buffer=%s rc=%d stderr=%r) — "
                    "buffer may persist until tmux server exits",
                    buffer_name,
                    cleanup.returncode,
                    cleanup.stderr[:200] if cleanup.stderr else b"",
                )


async def send_text_to_tmux(
    session_name: str,
    text: str,
    *,
    submit_enter: bool = True,
) -> None:
    """Deliver `text` to a tmux pane through bracketed paste.

    `submit_enter=False` skips the trailing Enter — used by the codex
    flow where `TmuxManager._safe_send_codex` first verifies that the
    paste landed (no modal popped, paste chip is visible) and only then
    presses Enter (or Tab, when codex is busy).
    """
    await send_paste(session_name, text)
    if submit_enter:
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            check=True,
        )


async def send_enter(session_name: str) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", session_name, "Enter"],
        check=True,
    )


async def send_tab(session_name: str) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", session_name, "Tab"],
        check=True,
    )


async def send_ctrl_u(session_name: str) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", session_name, "C-u"],
        check=True,
    )
