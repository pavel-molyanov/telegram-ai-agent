# Telegram AI Agent

[Русская версия](README.ru.md)

Open-source Telegram bot for running Claude Code or Codex from Telegram private
chats and forum topics. Each forum topic can have its own working directory,
prompt mode, engine, execution mode, stream mode, model, and MCP config.

The repository contains only the reusable bot runtime. It does not include
private assistant data, private prompts, runtime state, secrets, or
machine-specific deployment config.

## How It Works

The bot receives Telegram messages, checks `ALLOWED_USER_IDS`, downloads
attached media when needed, and forwards the request to Claude Code or Codex on
the same machine.

Two runtime choices are independent:

- `engine`: `claude` or `codex`.
- `exec_mode`: `subprocess` for one-off runs, or `tmux` for persistent TUI
  sessions that survive bot restarts.

Forum topics are isolated by Telegram `chat_id` and `thread_id`. The bot reads
`topic_config.json` to decide which directory, prompt mode, engine, MCP config,
and streaming style to use for each topic.

## Features

- Private chats and supergroup forum topics.
- Claude Code and Codex engines.
- Regular subprocess mode and persistent tmux mode.
- Stream modes: `verbose`, `live`, and `minimal`.
- TUI snapshots and controls with `/tui`.
- Session resume in tmux mode.
- Text, photos, documents, forwarded-message batches, and voice transcription.
- Topic-scoped `topic_config.json`.
- Bot MCP server for sending messages and files back to Telegram.
- English and Russian UI via `BOT_LANG`.
- systemd autostart with restart-on-failure.

## Requirements

- Python 3.12+
- `uv`
- Telegram bot token from `@BotFather`
- Your Telegram user ID from `@userinfobot`
- Claude Code and/or Codex CLI installed on the machine running the bot. The
  bot can work with either one installed. It prefers Claude Code by default,
  but switches a topic to Codex when Claude Code is missing and Codex exists.
- Optional: `tmux` for persistent TUI mode
- Optional: Deepgram API key for voice transcription

## Agent-Assisted Setup

If you are using Claude Code or Codex in this repository, ask the agent:

```text
Set up this Telegram bot using the bot-setup skill.
```

The setup skills are available for both agent runtimes:

- `.claude/skills/bot-setup/SKILL.md`
- `.codex/skills/bot-setup/SKILL.md`

Forum topic wiring is covered by:

- `.claude/skills/topic-setup/SKILL.md`
- `.codex/skills/topic-setup/SKILL.md`

The setup agent should ask which UI language you want (`en` or `ru`), whether
to use Claude Code or Codex by default, and whether to install a systemd service.

## Quickstart

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
cp .env.example .env
cp topic_config.example.json topic_config.json
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
ALLOWED_USER_IDS=[123456789]
BOT_LANG=en
DEEPGRAM_API_KEY=
PROJECT_ROOT=.
DEFAULT_CWD=.
FILE_CACHE_DIR=./data
TOPIC_CONFIG_PATH=./topic_config.json
TMUX_SESSIONS_DIR=./tmux_sessions
```

Set `BOT_LANG=ru` for Russian UI. Restart the bot after changing language.
For voice messages, create a Deepgram account, generate an API key, put it in
`DEEPGRAM_API_KEY`, and restart the bot. Leave it empty to disable voice
transcription.

Run locally:

```bash
uv run telegram-bot
```

Open Telegram, send `/start`, then write a message to the bot.

## Telegram Forum Setup

Private chat works for simple use. Project work is best done in a Telegram
supergroup with forum topics:

1. Create a bot with `@BotFather` and copy the token into `.env`.
2. Create a Telegram group or supergroup and enable forum topics in group
   settings.
3. Add the bot to the group as an admin with rights to read messages, send
   messages, manage topics, and send media/documents.
4. Create topics manually in Telegram, or ask a local Claude Code/Codex agent
   to use the `topic-setup` skill.

When a topic is created, the running bot auto-registers it in
`topic_config.json` with generic defaults. The topic can receive messages right
away, but it will use default settings until configured.

New topics prefer the `claude` engine. If Claude Code is missing but Codex is
installed, the bot starts with Codex and saves `engine=codex` for that topic.
If neither Claude Code nor Codex is installed, the bot still starts and tells
the user to install one of them.

## Autostart With Systemd

Use systemd on a server when the bot should start on boot and restart after a
crash.

```bash
APP_DIR="$(pwd)"
UV_BIN="$(command -v uv)"
USER_NAME="$(whoami)"
tmp_unit="$(mktemp)"
sed \
  -e "s#REPLACE_WITH_LINUX_USER#${USER_NAME}#g" \
  -e "s#REPLACE_WITH_ABSOLUTE_REPO_PATH#${APP_DIR}#g" \
  -e "s#REPLACE_WITH_UV_PATH#${UV_BIN}#g" \
  docs/systemd/telegram-bot.service.template >"${tmp_unit}"
sudo install -m 0644 "${tmp_unit}" /etc/systemd/system/telegram-bot.service
rm -f "${tmp_unit}"

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
sudo systemctl status telegram-bot
```

Logs:

```bash
journalctl -u telegram-bot -f
```

The service uses `Restart=on-failure` and `RestartSec=5`, so a crashed bot is
started again after five seconds. If it crashes more than five times in sixty
seconds, fix the error and run:

```bash
sudo systemctl reset-failed telegram-bot
sudo systemctl start telegram-bot
```

## Forum Topics

For supergroup forums, configure each topic in `topic_config.json`. You can edit
the file directly, use `/engine`, `/mode`, and `/stream` in Telegram, or ask a
local agent to run the `topic-setup` skill.

```json
{
  "topics": {
    "42": {
      "name": "My Project",
      "type": "project",
      "mode": "free",
      "cwd": "/absolute/path/to/project",
      "mcp_config": "/absolute/path/to/project/.mcp.json",
      "stream_mode": "live",
      "exec_mode": "tmux",
      "engine": "codex",
      "model": null
    }
  }
}
```

Use absolute paths for `cwd` and `mcp_config`. Set `mcp_config` to `null` to
use the generated bot MCP config.

Important fields:

- `type`: `assistant` or `project`.
- `mode`: `free` is the standard project/general prompt. `task` is included as
  an example of a second prompt mode; replace it with your own prompt file and
  topic mode if you want a different workflow.
- `stream_mode`: `verbose`, `live`, or `minimal`.
- `exec_mode`: `subprocess` for one-off assistant tasks where you do not need
  a persistent agent process, or `tmux` for full development sessions with
  persistent context, TUI snapshots, `/resume`, and `/tui`.
- `engine`: `claude` or `codex`.
- `model`: optional provider model override, or `null`.

The bot auto-registers manually created Telegram topics, but it does not guess
the correct project path. Use `topic-setup` when a topic should point at a
specific project directory, MCP config, engine, execution mode, stream mode, or
model.

## Commands

- `/start` - show the basic keyboard and verify the bot responds.
- `/new` - reset the current topic session. In tmux mode, the tmux process stays
  alive while logical context is reset.
- `/clear` - alias for `/new`.
- `/cancel` - cancel current processing. Use it before changing settings while
  a task is running.
- `/mode` - choose execution transport: regular subprocess for one-off tasks,
  or tmux for persistent development sessions.
- `/engine` - choose Claude Code or Codex for the current forum topic.
- `/stream` - choose progress delivery: `verbose`, `live`, or `minimal`.
- `/resume` - resume a saved tmux session for the topic working directory.
- `/tui` - show a tmux TUI snapshot and control buttons.
- `/tail` - alias for `/tui`.
- `/kill` - stop the active tmux session and free resources.

Recommended defaults:

- Short notes and one-off tasks: `/mode` -> regular, `/stream` -> minimal or
  live.
- Real development work: `/mode` -> tmux, `/stream` -> live.
- Old session no longer needed: `/kill`.

## Prompt Modes

Prompt files live in `src/telegram_bot/prompts/`:

- `default.md`
- `task-manager.md`

`default.md` is the standard project/general prompt used by `free`. The
`task-manager.md` prompt is a replaceable example showing how a second mode can
work. For your own workflow, add a public-safe prompt file and set a topic's
`mode` to that filename stem.

Keep public prompts generic. Do not commit private data, private workflows, or
secrets.

## MCP Bot Server

The bot MCP server lets an agent send messages or files back to the current
Telegram topic. Runtime code creates topic-scoped MCP configs automatically for
bot-launched sessions.

Both public prompt modes, `free` and `task`, allow the same generic bot MCP
send tools: `send_message`, `send_image`, and `send_document`.

Keep real `.mcp.json` files out of git because they may contain tokens or local
paths.

## Runtime Files

Never commit:

- `.env`
- `topic_config.json`
- `session_mapping.json`
- `channel_sessions.json`
- `tmux_sessions/`
- `data/`
- `.mcp*.json`
- `.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`
- `__pycache__/`, `*.pyc`, `*.pyo`

Only commit examples such as `.env.example`, `topic_config.example.json`, and
public-safe docs.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
PYTHONDONTWRITEBYTECODE=1 uv run python -c "import telegram_bot; import telegram_bot.__main__; print('ok')"
rm -rf .venv .ruff_cache .mypy_cache .pytest_cache
find src mcp-servers tests -type d -name __pycache__ -prune -exec rm -rf {} +
```

## Security Model

The bot is intended for trusted personal or small-team use. `ALLOWED_USER_IDS`
is the primary access control. Anyone who can talk to the bot can ask the
configured local agent CLI to operate in the configured working directory.

Do not expose the bot to untrusted users. Do not configure project topics to
directories you are not willing to let the agent read or edit.

## Author

Made by Pasha Molyanov. If this bot is useful to you, consider subscribing to my
Telegram channel about business, AI assistants, development, and launching
useful services: [@molyanov_blog](https://t.me/+zJ5qmSsoYediYzdi).

## License

MIT
