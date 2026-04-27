"""Inline keyboard for the /tui navigation panel.

Callback-data contract (colon-separated, ≤64 bytes for Telegram):

    ttui:<action>:<chat_id>:<thread_id>:<epoch>[:<kind>]

  - action: one of `_VALID_ACTIONS`
  - chat_id: int (can be negative for groups/channels)
  - thread_id: int, or `-` when the chat has no forum topics
  - epoch: first 8 hex chars of the CC session_id — lets the bot reject
    stale callbacks from a keyboard left over after `/new`
  - kind (optional): `panel` (default, regular /tui snapshot) or
    `modal` (modal-blocked / modal-idle alert). Absence of the field
    is read as `panel` — keyboards sent before UX-3 rollout keep
    working.

Worst-case payload is roughly
`ttui:<action>:<negative 14-digit chat_id>:<6-digit thread_id>:abcdef01:modal`
— about 49 bytes, well under the 64-byte Telegram limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_VALID_ACTIONS = frozenset(
    {
        "up",
        "dn",
        "lt",
        "rt",
        "ent",
        "bsp",
        "esc",
        "esc2",
        "tab",
        "btab",
        "cC",
        "cO",
        "cR",
        "cT",
        "num1",
        "num2",
        "num3",
        "num0",
        "refresh",
        "close",
    }
)

_EPOCH_RE = re.compile(r"[0-9a-f]{8}")

# Attached to modal-alert messages (user-send blocked, or watchdog-detected
# idle modal). Re-render of such a message must preserve the modal-alert
# layout (`<header>\n\n<pre>pane</pre>`) instead of collapsing to bare
# `<pre>pane</pre>` — see `handlers/tail.py::_rerender`.
KIND_PANEL = "panel"
KIND_MODAL = "modal"
_VALID_KINDS = frozenset({KIND_PANEL, KIND_MODAL})


@dataclass(frozen=True)
class TailCallback:
    """Parsed `ttui:...` callback_data payload."""

    action: str
    chat_id: int
    thread_id: int | None
    epoch: str
    kind: str = KIND_PANEL


def _cb(action: str, chat_id: int, thread_id: int | None, epoch: str, kind: str) -> str:
    thread = str(thread_id) if thread_id is not None else "-"
    base = f"ttui:{action}:{chat_id}:{thread}:{epoch}"
    # Omit the default `panel` suffix — keeps payloads short and keeps
    # pre-UX-3 keyboards (5-part) byte-for-byte identical.
    return base if kind == KIND_PANEL else f"{base}:{kind}"


def build_tail_keyboard(
    session_id: str,
    chat_id: int,
    thread_id: int | None,
    *,
    kind: str = KIND_PANEL,
) -> InlineKeyboardMarkup:
    """Build the /tui inline keyboard.

    Layout (4 rows):
      row 1 (arrows):     ⬆️  ⬇️  ⬅️  ➡️
      row 2 (input/esc):  ↩️  ⌫  Esc  Esc 2  Tab  ⇧Tab
      row 3 (digits):     1  2  3  0
      row 4 (CC ctrl):    Ctrl+C  Ctrl+O  Ctrl+R  Ctrl+T  🔄  Close

    `kind` tags the message this keyboard belongs to (`panel` or `modal`)
    so the re-render path can pick the correct layout. Modal-alert callers
    pass `kind=KIND_MODAL`; default is panel for `/tui` snapshots.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown keyboard kind: {kind!r}")
    epoch = session_id[:8]

    def cb(action: str) -> str:
        return _cb(action, chat_id, thread_id, epoch, kind)

    arrow_row = [
        InlineKeyboardButton(text="⬆️", callback_data=cb("up")),
        InlineKeyboardButton(text="⬇️", callback_data=cb("dn")),
        InlineKeyboardButton(text="⬅️", callback_data=cb("lt")),
        InlineKeyboardButton(text="➡️", callback_data=cb("rt")),
    ]

    input_row = [
        InlineKeyboardButton(text="↩️", callback_data=cb("ent")),
        InlineKeyboardButton(text="⌫", callback_data=cb("bsp")),
        InlineKeyboardButton(text="Esc", callback_data=cb("esc")),
        InlineKeyboardButton(text="Esc 2", callback_data=cb("esc2")),
        InlineKeyboardButton(text="Tab", callback_data=cb("tab")),
        InlineKeyboardButton(text="⇧Tab", callback_data=cb("btab")),
    ]

    digit_row = [
        InlineKeyboardButton(text="1", callback_data=cb("num1")),
        InlineKeyboardButton(text="2", callback_data=cb("num2")),
        InlineKeyboardButton(text="3", callback_data=cb("num3")),
        InlineKeyboardButton(text="0", callback_data=cb("num0")),
    ]

    control_row = [
        InlineKeyboardButton(text="Ctrl+C", callback_data=cb("cC")),
        InlineKeyboardButton(text="Ctrl+O", callback_data=cb("cO")),
        InlineKeyboardButton(text="Ctrl+R", callback_data=cb("cR")),
        InlineKeyboardButton(text="Ctrl+T", callback_data=cb("cT")),
        InlineKeyboardButton(text="🔄", callback_data=cb("refresh")),
        InlineKeyboardButton(text="Close", callback_data=cb("close")),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[arrow_row, input_row, digit_row, control_row])


def parse_tail_callback(data: str) -> TailCallback | None:
    """Parse a `ttui:...` callback_data string.

    Returns None on any validation failure: wrong prefix, wrong part count,
    non-integer chat_id/thread_id, malformed epoch, unknown action.
    """
    if not data.startswith("ttui:"):
        return None

    parts = data.split(":")
    # 5 parts: pre-UX-3 keyboards (legacy, treated as panel kind for
    # backward compatibility). 6 parts: new-style with explicit kind.
    if len(parts) not in (5, 6):
        return None

    action = parts[1]
    if action not in _VALID_ACTIONS:
        return None

    try:
        chat_id = int(parts[2])
    except ValueError:
        return None

    thread_raw = parts[3]
    if thread_raw == "-":
        thread_id: int | None = None
    else:
        try:
            thread_id = int(thread_raw)
        except ValueError:
            return None

    epoch = parts[4]
    if not _EPOCH_RE.fullmatch(epoch):
        return None

    if len(parts) == 6:
        kind = parts[5]
        if kind not in _VALID_KINDS:
            return None
    else:
        kind = KIND_PANEL

    return TailCallback(
        action=action,
        chat_id=chat_id,
        thread_id=thread_id,
        epoch=epoch,
        kind=kind,
    )
