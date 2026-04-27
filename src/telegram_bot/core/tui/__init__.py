"""tmux-TUI runtime helpers — pure modules, no TmuxManager state.

Stateful orchestration lives in `TmuxManager` (Wave 2). This package hosts
only pure helpers: path prediction, send-keys planning, pane capture
classification, transcript jsonl parsing, slash-command routing, and the
/tui inline keyboard.
"""
