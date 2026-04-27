#!/bin/bash
# Bot MCP Server launcher
# Reads TELEGRAM_BOT_TOKEN from PROJECT_DIR/.env, exposes it as BOT_TOKEN.

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR/../..}"

if [ -f "$PROJECT_DIR/.env" ]; then
  export $(grep -v '^#' "$PROJECT_DIR/.env" | grep -v '^$' | xargs)
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN not set in $PROJECT_DIR/.env" >&2
  exit 1
fi

export BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
exec "$PROJECT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"
