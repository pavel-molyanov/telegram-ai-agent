# Claude Instructions

Follow `AGENTS.md`. This is a public generic Telegram bot runtime, not a
personal assistant repository.

Before editing, inspect the relevant files and keep changes scoped. Do not copy
private prompts, private project knowledge, local runtime configs, secrets, or
machine-specific deployment files into this repository.

Load the public project knowledge skill before changing, debugging, or explaining how
this repository works:

- `.agents/skills/project-knowledge/SKILL.md`

Use `.agents/skills/bot-setup/SKILL.md` for installation, systemd autostart,
language, commands, and operator troubleshooting. Use
`.agents/skills/topic-setup/SKILL.md` for Telegram forum topic wiring.

Run the standard checks before final handoff:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
```
