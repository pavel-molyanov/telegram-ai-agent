# Telegram AI Agent

[Русская версия](README.ru.md)

Telegram AI Agent is an open-source Telegram bot for controlling Claude Code and
Codex CLI on your VPS. It turns Telegram into a remote interface for agentic
coding: open a topic for a project, send tasks from your phone, attach files or
voice notes, watch progress, resume old sessions, and drive the live terminal
UI when the agent needs input.

The repository contains the reusable public bot runtime only. It does not
contain private assistant data, private prompts, runtime state, real IDs,
tokens, or machine-specific deployment config.

## What You Can Do

- Run Claude Code or Codex from Telegram private chats or group forum topics.
- Keep one Telegram topic per project, workflow, or long-running agent context.
- Bind a topic to a directory on your VPS, for example
  `/home/user/projects/my-app`.
- Choose Claude Code or Codex per topic.
- Use a persistent `tmux` session for real development work, or a short-lived
  subprocess for simple one-off tasks.
- Send text, photos, documents, forwarded message batches, and optional voice
  messages.
- Use custom prompt modes for different workflows.
- Open a live TUI snapshot with `/tui` and press buttons for Enter, Esc, arrows,
  digits, refresh, and close.
- Resume saved sessions by replying to previous bot messages or with `/resume`.
- Let the agent send messages, images, and documents back to Telegram through
  the bundled bot MCP server.

## How It Works

The bot runs on the same machine as Claude Code and/or Codex CLI. Telegram is
only the control surface. When you send a message, the bot:

1. checks that your Telegram user ID is allowed;
2. downloads attached media when needed;
3. resolves the current chat or forum topic settings;
4. sends the prompt to Claude Code or Codex in the configured working directory;
5. streams progress and the final answer back to Telegram.

Forum topics are isolated by Telegram `chat_id` and `thread_id`. Each topic can
have its own:

- `cwd`: project directory on the VPS;
- `mode`: prompt mode;
- `engine`: `claude` or `codex`;
- `exec_mode`: `tmux` or `subprocess`;
- `stream_mode`: `verbose`, `live`, or `minimal`;
- `mcp_config`: optional MCP config for the agent;
- `model`: optional provider model override.

## Requirements

You need a Linux machine or VPS where the bot and agent CLIs will run.

- Python 3.12+
- `uv`
- Telegram bot token from `@BotFather`
- Your numeric Telegram user ID from `@userinfobot`
- Claude Code CLI and/or Codex CLI installed for the same Linux user that runs
  the bot
- `tmux` for persistent development sessions
- Optional: Deepgram API key for voice transcription

The bot can run with only one agent CLI installed. It prefers Claude Code by
default, but if Claude Code is missing and Codex is available, a topic can run
with Codex.

Check the server user before continuing:

```bash
python3 --version
uv --version
command -v tmux
command -v claude || true
command -v codex || true
```

At least one of `claude` or `codex` must exist. Also make sure the CLI is
authenticated or configured for the same Linux user that will run the bot.

## Setup Option A: Agent-Assisted

If you already have Claude Code or Codex on the VPS, this is the easiest path.

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
```

Then open Claude Code or Codex in this repository and ask:

```text
Set up this Telegram bot using the bot-setup skill.
```

The repository ships setup skills for both runtimes:

- `.claude/skills/bot-setup/SKILL.md`
- `.codex/skills/bot-setup/SKILL.md`

For forum topics, ask:

```text
Create and configure Telegram forum topics using the topic-setup skill.
```

The topic setup skills live in:

- `.claude/skills/topic-setup/SKILL.md`
- `.codex/skills/topic-setup/SKILL.md`

The agent should ask for your UI language, default engine, execution mode, and
whether to install a systemd service.

## Setup Option B: Manual

Clone and install:

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
cp .env.example .env
cp topic_config.example.json topic_config.json
chmod 600 .env topic_config.json
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
CC_MAX_TURNS=100
CC_INACTIVITY_KILL_SEC=1800
```

Notes:

- `TELEGRAM_BOT_TOKEN`: create a bot in `@BotFather`.
- `ALLOWED_USER_IDS`: JSON array of Telegram user IDs allowed to use the bot.
- `BOT_LANG`: `en` or `ru`. Restart the bot after changing it.
- `DEFAULT_CWD`: default working directory for unconfigured topics.
- `DEEPGRAM_API_KEY`: leave empty if you do not need voice messages.

Run in the foreground:

```bash
uv run telegram-bot
```

Open Telegram and send `/start`. Before installing systemd, send one normal
message and check that Claude Code or Codex answers. This catches missing CLI
auth, wrong `PATH`, and bad project paths while logs are still in your terminal.

## Telegram Bot And Group Setup

Private chat works for simple use. For real project work, use a Telegram
supergroup with forum topics.

1. In `@BotFather`, create a bot and copy its token to `.env`.
2. In `@BotFather`, use `/setprivacy` and disable privacy if you want the bot
   to receive normal non-command messages in group topics.
3. Create a Telegram group or supergroup.
4. Enable forum topics in the group settings.
5. Add the bot to the group.
6. Make the bot an admin with rights to read messages, send messages, manage
   topics, and send media/documents.
7. Create topics manually, or use the `topic-setup` skill from Claude
   Code/Codex.

Each topic is a separate agent workspace. You can have one topic for a product
repo, another for a landing page, another for task management, another for
writing posts, and so on. The public repo includes a generic `free` prompt and a
replaceable `task` example. Your own installation can add any prompt modes you
need.

When a new forum topic appears, the running bot registers it in
`topic_config.json` with default settings. It can receive messages immediately,
but it will use defaults until you configure it.

The public `topic_config.json` keys topics by Telegram `message_thread_id`, so
the simplest and recommended setup is one forum group per bot config. To find a
topic ID, start the bot, create or rename a topic, send a message there, then
open the generated `topic_config.json` and edit the new entry.

## Topic Configuration

You can edit `topic_config.json` directly, use `/engine`, `/mode`, and `/stream`
inside Telegram forum topics, or ask the `topic-setup` skill to configure
topics.

Terminology is important: config field `mode` means prompt mode. Telegram
command `/mode` changes execution mode, stored as `exec_mode`.

Example:

```json
{
  "topics": {
    "42": {
      "name": "My App",
      "type": "project",
      "mode": "free",
      "cwd": "/home/user/projects/my-app",
      "mcp_config": null,
      "stream_mode": "live",
      "exec_mode": "tmux",
      "engine": "codex",
      "model": null
    }
  }
}
```

Fields:

- `name`: human-readable label.
- `type`: `assistant` or `project`.
- `mode`: prompt mode. Public modes are `free` and `task`.
- `cwd`: absolute project directory, or `null` to use `DEFAULT_CWD`.
- `mcp_config`: absolute MCP config path, or `null` for bot-generated MCP
  config.
- `stream_mode`: `verbose`, `live`, or `minimal`.
- `exec_mode`: `tmux` or `subprocess`.
- `engine`: `claude` or `codex`.
- `model`: optional model override, or `null`.

Use absolute paths for `cwd` and `mcp_config`. Set `mcp_config` to `null`
unless you already have a real MCP config file for that project. Do not commit
your real `topic_config.json`.

## Prompt Modes

Prompt files live in `src/telegram_bot/prompts/`.

Public modes:

- `free`: the default general/project prompt, backed by `default.md`.
- `task`: a small replaceable task-management example, backed by
  `task-manager.md`.

For no-code customization, edit or replace `task-manager.md` and use
`"mode": "task"` in selected topics. If you want a new mode name such as
`blog`, add the prompt file and extend the runtime tool mapping for that mode in
code; otherwise the agent may not have an allowed tool list. Keep private data,
secrets, personal workflows, and real customer context out of the public
repository.

## Execution Modes

`/mode` chooses how the agent process runs.

### tmux

Use `tmux` for real development work.

The bot starts a persistent terminal session and sends your Telegram messages
directly into Claude Code or Codex TUI. The session keeps context, can survive
bot restarts, and can be inspected with `/tui`.

Use this when:

- you are editing a codebase;
- you want the agent to remember the long-running context;
- the agent may ask permission questions or show interactive menus;
- you want `/resume` and reply-to-session behavior.

`tmux` consumes resources while the session is alive. Use `/kill` when you no
longer need it.

### subprocess

Use `subprocess` for short tasks.

Each message starts a fresh CLI process, receives the answer, and exits. This is
good for simple questions, notes, small transformations, and tasks where you do
not want a persistent TUI session running in the background.

Private chats use default settings and are good for simple use. Per-topic
controls such as `/mode`, `/stream`, `/engine`, and `/resume` work in forum
topics, because they need a topic-specific config entry.

## Stream Modes

`/stream` controls how much progress the bot sends back to Telegram.

- `verbose`: sends detailed progress as separate messages. Useful for debugging.
- `live`: keeps one editable progress message and then sends the final answer.
  This is the best default for most project work.
- `minimal`: focuses on final answers. Useful when you want a quieter chat.

## TUI Mode

`/tui` opens a snapshot of the live tmux pane and attaches control buttons.

This exists because Claude Code and Codex CLIs are terminal applications. They
can show permission prompts, menus, confirmations, model pickers, and other
interactive UI. The bot can write into the terminal and read the transcript, but
sometimes you need to see and steer the TUI directly.

Use `/tui` to:

- inspect what the agent is doing;
- press Enter, Esc, arrows, Tab, Backspace, Ctrl+C, or digits;
- handle permission dialogs;
- handle startup modals (e.g. Codex's update prompt or trust dialog) — when
  the agent shows a modal during initial spawn and the bot can't push input
  through, you'll see "engine started but input is blocked, use /tui";
  open `/tui` and dismiss the modal with the keyboard;
- recover from a stuck-looking interactive state.

`/tail` is a legacy alias for `/tui`.

## Reply, Resume, And Sessions

The bot records which agent session produced each answer. If you reply to an
old bot message, the bot can route your new message back to the matching
session. This is useful when one Telegram topic has multiple historical
sessions.

In tmux mode, `/resume` shows saved sessions for the topic working directory and
lets you switch back to one of them. If the target session belongs to a
different engine or execution mode, the bot can switch the topic settings
before resuming. If a live tmux session must be replaced, it can be stopped as
part of that switch.

Slash commands are special in tmux topics: non-bot commands such as `/model` or
`/compact` are sent to the live TUI, not to the replied-to historical session.

`/clear` starts fresh logical context for the current topic. In tmux mode the
bot resets or respawns the tmux session depending on the current state. `/new`
still exists as a legacy alias, but `/clear` is the command shown in the menu.

## Commands

- `/start`: check that the bot responds and show the basic keyboard.
- `/clear`: reset the current topic session.
- `/cancel`: cancel current processing.
- `/language`: show or switch UI language, for example `/language ru`.
- `/mode`: forum topics only; choose `tmux` or `subprocess`. Switching from
  `tmux` to `subprocess` stops the active tmux session.
- `/engine`: forum topics only; choose Claude Code or Codex. Changing engine
  resets the active session.
- `/stream`: forum topics only; choose `verbose`, `live`, or `minimal`.
- `/resume`: forum topics only; resume a saved tmux session for the current
  topic working directory.
- `/tui`: show and control the live tmux TUI.
- `/tail`: legacy alias for `/tui`.
- `/kill`: stop the active tmux session and free resources.

Recommended defaults:

- Real development: `/mode` -> `tmux`, `/stream` -> `live`.
- Short one-off tasks: `/mode` -> `subprocess`, `/stream` -> `minimal` or
  `live`.
- Old session no longer needed: `/kill`.

## MCP Bot Server

The bundled MCP server lets the agent send content back to the current Telegram
topic. Bot-launched sessions receive topic-scoped MCP configuration
automatically.

Public prompt modes allow these generic bot tools:

- `send_message`
- `send_image`
- `send_document`

Keep real `.mcp.json` files out of git. They may contain tokens or local paths.

If `mcp_config` is `null`, the bot generates a runtime MCP config containing
the Telegram bot server and the current topic routing. If `mcp_config` points to
an existing file, the bot uses it as the base config and injects the Telegram
bot server into the runtime copy. If the path does not exist, use `null` or fix
the path before relying on project-specific MCP tools.

## Autostart With Systemd

On a VPS, run the bot as a systemd service so it starts after reboot and
restarts after crashes.

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

Systemd may have a smaller `PATH` than your shell. Before relying on the
service, verify that the service user can run `uv`, `tmux`, and at least one of
`claude` or `codex`. If the CLIs live in a user-local directory, add an
`Environment=PATH=...` line to the unit or use absolute paths.

If systemd stops retrying after repeated crashes:

```bash
sudo systemctl reset-failed telegram-bot
sudo systemctl start telegram-bot
```

## Runtime Files

Do not commit runtime files:

- `.env`
- `topic_config.json`
- `session_mapping.json`
- `channel_sessions.json`
- `tmux_sessions/`
- `data/`
- `.mcp*.json`
- `.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`
- `__pycache__/`, `*.pyc`, `*.pyo`

Commit only public-safe examples and docs, such as `.env.example`,
`topic_config.example.json`, and README files.

## Security Model

This bot is for trusted personal or small-team use. `ALLOWED_USER_IDS` is the
main access control. Anyone who can talk to the bot can ask the configured local
agent CLI to operate in the configured working directory, including file edits
and shell/tool actions allowed by that CLI.

Do not expose the bot to untrusted users. Do not point project topics at
directories you are not willing to let the agent read or edit. Prefer running
the bot under a dedicated low-privilege Linux user. If the bot token leaks,
rotate it in `@BotFather`.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
PYTHONDONTWRITEBYTECODE=1 uv run python -c "import telegram_bot; import telegram_bot.__main__; print('ok')"
```

## Feedback

Issues, bug reports, and ideas are welcome. Open a GitHub issue if something is
unclear, broken, or missing.

Made by Pasha Molyanov. I write about business, AI assistants, development, and
launching useful services in my Telegram channel:
[@molyanov_blog](https://t.me/+zJ5qmSsoYediYzdi).

## License

MIT
