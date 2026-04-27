"""Render the Telegram alert shown when send_direct is blocked by a modal.

Pure function — no I/O. Returns the (HTML text, inline keyboard) pair
ready to pass to `aiogram.Bot.send_message(parse_mode="HTML", reply_markup=...)`.

The inline keyboard is the same one built by `/tui`: arrow keys, Enter,
Esc/Esc-Esc, digits, refresh, close. Reusing it means the user already
knows how to operate this panel — it's the same tool they've used for
manual TUI control.

Message layout:
  <header>                   — i18n'd, names the blocked prompt (truncated)
  <pane snapshot as <pre>>   — HTML-escaped, capped like /tui (3900 chars)
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardMarkup

from telegram_bot.core.messages import t
from telegram_bot.core.tui.capture import escape_pane_for_html
from telegram_bot.core.tui.tail_keyboard import KIND_MODAL, build_tail_keyboard

# Telegram's hard cap is 4096 chars per message. 120 chars of prompt
# preview is enough to identify which message got blocked (fits one
# Telegram message line) while leaving safe headroom.
MAX_PROMPT_PREVIEW_CHARS = 120

# Telegram's absolute text limit. We cap final output just under it so
# a near-boundary assembly doesn't overflow after a UTF-8-aware recount
# inside aiogram. 4080 = 4096 - 16 bytes of safety margin; cheap.
_TG_MSG_LIMIT = 4080
# Pre-produced byte overhead that _render wraps around the escaped pane
# body: `<pre>` + `</pre>` + `\n\n` separator between header and pane.
_PRE_WRAPPER_AND_SEP_CHARS = len("<pre>") + len("</pre>") + len("\n\n")
# Upper bound on pane `<pre>` body so it looks like /tui even without a
# header. Matches handlers/tail.py:_PANE_MAX_CHARS — deliberately kept as
# an upper ceiling, the real cap is recomputed dynamically vs. the header.
_PANE_MAX_CHARS_CEILING = 3900
_TRUNCATION_PREFIX = "... (truncated)\n"


def _format_prompt_preview(prompt: str) -> str:
    """Head + ellipsis for the header. HTML-escape the result — the prompt
    is untrusted user text and will land inside a <code>...</code> tag."""
    head = prompt[: MAX_PROMPT_PREVIEW_CHARS - 1]
    suffix = "…" if len(prompt) > MAX_PROMPT_PREVIEW_CHARS - 1 else ""
    return html.escape(head + suffix, quote=False)


def _format_pane_html(raw_pane: str, *, max_body_chars: int) -> str:
    """HTML-escape the pane and cap its body to `max_body_chars`. Caller
    is responsible for computing `max_body_chars` so the full assembled
    message stays under Telegram's 4096-char limit — see `_assemble`.

    Truncation keeps the TAIL of the pane (`escaped[-n:]`) because the
    footer with the modal's dismiss options is the load-bearing context
    for the user; the older scrollback above is filler."""
    escaped = escape_pane_for_html(raw_pane)
    if max_body_chars <= 0:
        # Header alone already ate the budget — return a minimal body so
        # the user still sees *something* identifying the pane state.
        return "<pre>…</pre>"
    if len(escaped) > max_body_chars:
        remaining = max_body_chars - len(_TRUNCATION_PREFIX)
        if remaining <= 0:
            escaped = _TRUNCATION_PREFIX[:max_body_chars]
        else:
            escaped = _TRUNCATION_PREFIX + escaped[-remaining:]
    return f"<pre>{escaped}</pre>"


def _pane_budget_for(header: str) -> int:
    """Return how many chars the <pre>…</pre> body can hold given that
    `header` + separator + <pre></pre> wrapper must already fit under
    Telegram's 4096-char limit.

    Capped at `_PANE_MAX_CHARS_CEILING` so short headers (idle-alert)
    don't produce 4000-char panes that dwarf the alert itself."""
    overhead = len(header) + _PRE_WRAPPER_AND_SEP_CHARS
    budget = _TG_MSG_LIMIT - overhead
    return max(0, min(budget, _PANE_MAX_CHARS_CEILING))


def _assemble(header: str, pane: str) -> str:
    """Compose `<header>\\n\\n<pre>{pane_body}</pre>` with the pane body
    shrunk to fit under Telegram's limit regardless of how long the
    header turned out to be (i18n variants, long prompt previews)."""
    body = _format_pane_html(pane, max_body_chars=_pane_budget_for(header))
    return f"{header}\n\n{body}"


def render_modal_idle_alert(
    *,
    pane: str,
    session_id: str,
    chat_id: int,
    thread_id: int | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the alert shown when the watchdog finds a modal with no
    user message in flight. Same layout as `render_modal_alert` minus
    the prompt preview — there is no user prompt to echo back."""
    header = t("ui.modal_idle_detected")
    text = _assemble(header, pane)
    keyboard = build_tail_keyboard(
        session_id=session_id,
        chat_id=chat_id,
        thread_id=thread_id,
        kind=KIND_MODAL,
    )
    return text, keyboard


def render_modal_alert(
    *,
    prompt: str,
    pane: str,
    session_id: str,
    chat_id: int,
    thread_id: int | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the alert payload.

    Args:
      prompt: the user message that was not sent.
      pane: raw `tmux capture-pane -p` output (unescaped).
      session_id: full UUID4 — its first 8 hex chars become the keyboard
        epoch so stale presses after a session swap are rejected.
      chat_id, thread_id: Telegram routing, pinned into callback_data.

    Returns:
      (html_text, keyboard) — ready for send_message(parse_mode="HTML").
    """
    header = t("ui.modal_blocked_header", prompt=_format_prompt_preview(prompt))
    text = _assemble(header, pane)
    keyboard = build_tail_keyboard(
        session_id=session_id,
        chat_id=chat_id,
        thread_id=thread_id,
        kind=KIND_MODAL,
    )
    return text, keyboard
