# Release Safety

The public repository must contain only reusable bot runtime code and generic
documentation.

Allowed release paths:

- `README.md`
- `README.ru.md`
- `.env.example`
- `.gitignore`
- `LICENSE`
- `docs/`
- `.github/`
- `pyproject.toml`
- `uv.lock`
- `src/`
- `mcp-servers/`
- `topic_config.example.json`
- `.claude/`
- `.codex/`
- `tests/`
- `AGENTS.md`
- `CLAUDE.md`

Required public skills in both `.claude/skills/` and `.codex/skills/`:

- `project-knowledge`
- `bot-setup`
- `topic-setup`

Never use `git add -A` for release staging. Review every staged path.

Blockers:

- real tokens, API keys, passwords, or private IDs;
- private assistant code or prompts;
- personal paths;
- runtime files;
- generated Python artifacts;
- private deployment assumptions;
- unsafe symlinks;
- private data anywhere in git history.

Before publish, run leak scans, gitleaks on working tree and history,
clean-clone validation, public CI, private regression QA, and public staging
Telegram QA.
