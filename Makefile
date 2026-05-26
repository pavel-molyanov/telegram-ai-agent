.DEFAULT_GOAL := help

TMUX_SESSION ?= tg-bot
LOG_FILE     ?= .tg-bot.log
PROC_PATTERN ?= telegram-bot

.PHONY: help install check run start stop restart status attach logs clean

help: ## Show this help
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "Vars (override on CLI):"
	@echo "  TMUX_SESSION=$(TMUX_SESSION)  LOG_FILE=$(LOG_FILE)  PROC_PATTERN=$(PROC_PATTERN)"

install: ## uv sync (install deps)
	@uv sync

check: ## ruff + mypy + pytest
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy src/ mcp-servers/bot/server.py
	@uv run pytest

run: ## Run bot in foreground
	@exec uv run telegram-bot

start: ## Start bot in background tmux session
	@if pgrep -f '$(PROC_PATTERN)' >/dev/null 2>&1; then \
		echo "already running:"; pgrep -af '$(PROC_PATTERN)'; exit 0; \
	fi
	@tmux new-session -d -s '$(TMUX_SESSION)' "uv run telegram-bot 2>&1 | tee '$(LOG_FILE)'"
	@sleep 2
	@if pgrep -f '$(PROC_PATTERN)' >/dev/null 2>&1; then \
		echo "started: $$(pgrep -f '$(PROC_PATTERN)' | tr '\n' ' ') (log: $(LOG_FILE), session: $(TMUX_SESSION))"; \
	else \
		echo "failed to start; try: make logs"; exit 1; \
	fi

stop: ## Stop bot (TERM, then KILL after 5s)
	@pids="$$(pgrep -f '$(PROC_PATTERN)' || true)"; \
	if [ -z "$$pids" ]; then \
		echo "not running"; \
	else \
		echo "stopping $$pids"; \
		kill $$pids 2>/dev/null || true; \
		for i in 1 2 3 4 5; do \
			pgrep -f '$(PROC_PATTERN)' >/dev/null 2>&1 || break; \
			sleep 1; \
		done; \
		pgrep -f '$(PROC_PATTERN)' >/dev/null 2>&1 && kill -9 $$(pgrep -f '$(PROC_PATTERN)') 2>/dev/null || true; \
	fi
	@if tmux has-session -t '$(TMUX_SESSION)' 2>/dev/null; then \
		tmux kill-session -t '$(TMUX_SESSION)'; \
		echo "tmux session $(TMUX_SESSION) killed"; \
	fi
	@echo "stopped"

restart: ## Stop then start
	@$(MAKE) --no-print-directory stop
	@$(MAKE) --no-print-directory start

status: ## Show bot process and tmux session state
	@echo "process:"
	@pgrep -af '$(PROC_PATTERN)' || echo "  not running"
	@echo "tmux session $(TMUX_SESSION):"
	@if tmux has-session -t '$(TMUX_SESSION)' 2>/dev/null; then echo "  yes"; else echo "  no"; fi

attach: ## Attach to bot's tmux session
	@if tmux has-session -t '$(TMUX_SESSION)' 2>/dev/null; then \
		tmux attach -t '$(TMUX_SESSION)'; \
	else \
		echo "no tmux session; try: make start"; exit 1; \
	fi

logs: ## Tail log file
	@if [ -f '$(LOG_FILE)' ]; then \
		tail -f '$(LOG_FILE)'; \
	else \
		echo "log file not found ($(LOG_FILE)); bot may be running foreground"; exit 1; \
	fi

clean: ## Remove caches (keeps .venv, .env, logs, .codegraph, .claude, .nessy)
	@rm -rf .pytest_cache .ruff_cache .mypy_cache
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "cleaned"
