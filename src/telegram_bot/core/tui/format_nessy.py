"""Nessy-specific pane formatting for Telegram <pre> output.

Applies in order:
  1. Collapse box frames (╭──╮ / │ text │ / ╰──╯) into ⏺ ... header + indented lines.
  2. Strip the prompt placeholder ("Type your message...") from prompt lines.
  3. Delegate to capture.escape_pane_for_html for separator truncation,
     HTML escaping, and C0 control stripping.
"""

from __future__ import annotations

import re

from telegram_bot.core.tui.capture import escape_pane_for_html

# ╭────╮ top frame, ╰────╯ bottom frame, │ text │ content line
_BOX_TOP_RE = re.compile(r"^╭[─-▟]{39,}╮$")
_BOX_BOTTOM_RE = re.compile(r"^╰[─-▟]{39,}╯$")
_BOX_CONTENT_RE = re.compile(r"^│(.*)│$")
# Strips grey placeholder text after the prompt character (U+276F or plain >)
_PROMPT_PLACEHOLDER_RE = re.compile(r"([\u276f>])(\s+Type your message[^\n]*)")


def _collapse_boxes(text: str) -> str:
    """Replace each ╭──╮ / │ text │ / ╰──╯ box with ⏺ ... header + indented content lines."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_box = False
    box_leading = ""
    box_line_ending = ""
    box_contents: list[str] = []

    for line in lines:
        stripped = line.rstrip("\n\r")
        line_ending = line[len(stripped) :]
        core = stripped.strip()
        leading = stripped[: len(stripped) - len(stripped.lstrip())]

        if not in_box:
            if _BOX_TOP_RE.match(core):
                in_box = True
                box_leading = leading
                box_line_ending = line_ending
                box_contents = []
            else:
                out.append(line)
        else:
            if m := _BOX_CONTENT_RE.match(core):
                box_contents.append(m.group(1).strip())
            elif _BOX_BOTTOM_RE.match(core):
                in_box = False
                contents = box_contents[:]
                box_contents = []
                out.append(box_leading + "⏺ ..." + box_line_ending)
                for c in contents:
                    if c:
                        out.append(box_leading + "  " + c + box_line_ending)
            else:
                # Unexpected line inside box — flush as plain content
                in_box = False
                for c in box_contents:
                    out.append(box_leading + "│ " + c + " │" + box_line_ending)
                out.append(line)

    for c in box_contents:
        out.append(box_leading + "│ " + c + " │" + box_line_ending)

    return "".join(out)


def _strip_prompt_placeholder(text: str) -> str:
    """Remove grey 'Type your message or @path/to/file' hint from prompt lines."""
    return _PROMPT_PLACEHOLDER_RE.sub(r"\1", text)


def format_pane_html(raw_pane: str) -> str:
    """Format a Nessy tmux pane snapshot for Telegram <pre> output."""
    text = _collapse_boxes(raw_pane)
    text = _strip_prompt_placeholder(text)
    return escape_pane_for_html(text)
