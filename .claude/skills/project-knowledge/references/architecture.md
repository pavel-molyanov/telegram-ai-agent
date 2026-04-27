# Architecture

The bot runtime is split into:

- `src/telegram_bot/__main__.py` - public entry point, aiogram wiring, shutdown.
- `src/telegram_bot/core/handlers/` - Telegram command, text, media, voice, forward, topic, and TUI handlers.
- `src/telegram_bot/core/services/` - session management, provider adapters, topic config, streaming, tmux, resume, MCP runtime, and transcription.
- `src/telegram_bot/core/tui/` - tmux TUI capture, modal detection, keyboard controls, routing, and transcript helpers.
- `mcp-servers/bot/` - MCP server that lets an agent send messages or files back to Telegram.
- `src/telegram_bot/prompts/` - generic public prompt modes.

Two independent runtime axes are important:

- `engine`: `claude` or `codex`.
- `exec_mode`: `subprocess` or `tmux`.

Engine selection is availability-aware: Claude Code is preferred by default,
Codex is used when Claude Code is missing and Codex exists, and the bot remains
online with a user-facing install message when neither CLI is available.

`stream_mode` controls Telegram progress delivery:

- `verbose`: separate progress messages.
- `live`: editable progress buffer plus final answer.
- `minimal`: final-answer oriented delivery.

Forum topics are isolated by `(chat_id, thread_id)`. Session mappings and tmux
state are runtime files and must not be committed.
