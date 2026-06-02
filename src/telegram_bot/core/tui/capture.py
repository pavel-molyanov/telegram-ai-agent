"""Pane capture helpers: TUI state detectors, HTML escaping, readiness wait.

State detectors are substring-matching predicates on `tmux capture-pane -p`
output. `escape_pane_for_html` prepares a snapshot for Telegram `<pre>`
blocks — it HTML-escapes `&<>` and strips C0 control bytes (while keeping
`\\t` and `\\n`) to avoid Telegram rejecting the message.

`await_prompt_ready` is a net-new async polling wrapper used by the tmux
runner at session spawn. It auto-accepts the trust dialog at most once, then
polls until the prompt marker appears. On timeout it sends one fallback
Enter, polls for another 5s, then kills the session and returns False.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import subprocess
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Real CC 2.1.114 trust-dialog text observed in PoC on 2026-04-19.
TRUST_DIALOG_SUBSTRINGS = (
    "trust this folder",
    "Do you trust the contents of this directory",  # Codex / older CC, fallback
)

_C0_STRIP_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_PROMPT_LINE_RE = re.compile(r"(?m)^[❯>]\s")
# Box-drawing separator characters: U+2500-U+259F plus common dash/hyphen.
# Lines longer than 20 chars are truncated; box-frame collapsing is in format_nessy.py.
_SEPARATOR_LINE_RE = re.compile(r"^[\u2500-\u259F\-]{21,}$")
_MAX_SEP_WIDTH = 20


def clean_separator_lines(text: str) -> str:
    """Truncate long pure separator lines to 20 chars.

    Handles leading whitespace (tmux padding). Box frame collapsing
    (\u256d\u2500\u2500\u256e / \u2502 text \u2502 / \u2570\u2500\u2500\u256f) is
    provider-specific \u2014 see format_nessy.py.
    """
    lines = text.splitlines(keepends=True)
    cleaned: list[str] = []
    for line in lines:
        stripped = line.rstrip("\n\r")
        line_ending = line[len(stripped) :]
        core = stripped.strip()
        leading = stripped[: len(stripped) - len(stripped.lstrip())]
        if _SEPARATOR_LINE_RE.match(core):
            cleaned.append(leading + core[:_MAX_SEP_WIDTH] + line_ending)
        else:
            cleaned.append(line)
    return "".join(cleaned)


_POLL_INTERVAL_SEC = 0.5
_FALLBACK_POLL_BUDGET_SEC = 5.0


def is_trust_dialog(pane_text: str) -> bool:
    return any(s in pane_text for s in TRUST_DIALOG_SUBSTRINGS)


def is_prompt_ready(pane_text: str) -> bool:
    if _PROMPT_LINE_RE.search(pane_text):
        return True
    return any(m in pane_text for m in ("start a new conversation", "/help"))


def escape_pane_for_html(pane_text: str) -> str:
    """Prepare a raw `tmux capture-pane -p` snapshot for Telegram `<pre>`.

    HTML-escape `&<>` (quote=False keeps `'` and `"` as-is since they are
    safe inside `<pre>`), then strip C0 control bytes `[\\x00-\\x08\\x0b-\\x1f]`
    plus DEL `\\x7f` which Telegram rejects. `\\t` (0x09) and `\\n` (0x0a)
    are preserved.
    """
    cleaned = clean_separator_lines(pane_text)
    escaped = html.escape(cleaned, quote=False)
    return _C0_STRIP_RE.sub("", escaped)


def _capture_pane(session_name: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", f"={session_name}:", "-p", "-S", "-200"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _send_enter(session_name: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"={session_name}:", "Enter"],
        check=True,
    )


def _kill_session(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={session_name}"],
        check=False,
    )


async def await_prompt_ready(
    session_name: str,
    timeout: float = 30.0,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Poll a tmux session until the Claude CLI prompt is ready.

    Polls `tmux capture-pane` every 500ms. Exit conditions:
      (a) trust-dialog detected + not yet handled → send one Enter, flip
          `trust_handled = True`, keep polling.
      (b) prompt-ready detected → return True.
      (c) deadline reached → send one fallback Enter, poll for 5 more
          seconds; if still not ready → `tmux kill-session` + return False.

    `clock` is injectable for tests (default `time.monotonic`). Allows
    sharing a deadline with `_spawn_tmux` (Wave 2, Decision 7 shared budget).
    Any `subprocess.CalledProcessError` from capture-pane means tmux died
    — return False immediately.

    Due to the 5s fallback window, the minimum wall-time to a False result
    is ~5s even if `timeout` is smaller.
    """
    import logging

    logger = logging.getLogger(__name__)

    clock = clock or time.monotonic
    deadline = clock() + timeout
    trust_handled = False
    pane = ""  # captured in loop; initialized so the timeout log never UnboundLocalError.

    poll_count = 0
    while clock() < deadline:
        poll_count += 1
        try:
            pane = await asyncio.to_thread(_capture_pane, session_name)
            if poll_count <= 3 or poll_count % 10 == 0:
                logger.info(
                    "TUI_IO: capture-pane poll=%d session=%s pane_len=%d pane_preview=%r",
                    poll_count,
                    session_name,
                    len(pane),
                    pane[:100] if pane else "",
                )
        except subprocess.CalledProcessError as e:
            logger.warning(
                "TUI_IO: capture-pane CalledProcessError poll=%d session=%s returncode=%d",
                poll_count,
                session_name,
                e.returncode,
            )
            return False

        if is_trust_dialog(pane) and not trust_handled:
            await asyncio.to_thread(_send_enter, session_name)
            trust_handled = True
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            continue

        if is_prompt_ready(pane):
            logger.info("TUI_IO: prompt ready poll=%d session=%s", poll_count, session_name)
            return True

        await asyncio.sleep(_POLL_INTERVAL_SEC)

    # Main loop exhausted — one fallback Enter, then 5s grace window.
    try:
        await asyncio.to_thread(_send_enter, session_name)
    except subprocess.CalledProcessError:
        return False

    fallback_deadline = clock() + _FALLBACK_POLL_BUDGET_SEC
    while clock() < fallback_deadline:
        try:
            pane = await asyncio.to_thread(_capture_pane, session_name)
        except subprocess.CalledProcessError:
            return False
        if is_prompt_ready(pane):
            return True
        await asyncio.sleep(_POLL_INTERVAL_SEC)

    # Elevated to WARNING with last pane snippet — session won't start, user
    # sees generic error; this log is the only clue what CC was stuck on.
    last_lines = "\n".join(pane.splitlines()[-10:]) if pane else "<empty>"
    logger.warning(
        "TUI_IO: readiness timeout for session=%s after %.1fs, killing. Last pane lines:\n%s",
        session_name,
        timeout,
        last_lines,
    )
    await asyncio.to_thread(_kill_session, session_name)
    return False
