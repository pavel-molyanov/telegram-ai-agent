"""Claude Code pane formatting for Telegram <pre> output.

Currently delegates to the shared escape_pane_for_html (separator
truncation + HTML escape + C0 strip). Add Claude-specific overrides here
when needed.
"""

from telegram_bot.core.tui.capture import escape_pane_for_html as format_pane_html

__all__ = ["format_pane_html"]
