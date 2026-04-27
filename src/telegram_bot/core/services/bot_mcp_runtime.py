"""Topic-scoped MCP config generation for bot-launched agent sessions."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

_RUNTIME_FILE_MODE = 0o600
_RUNTIME_DIR_MODE = 0o700


def default_bot_mcp_config(project_root: str | Path) -> Path:
    """Return the default bot MCP config path for a project root."""
    return Path(project_root) / ".mcp.bot.json"


def _load_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load MCP config %s", path, exc_info=True)
        return {"mcpServers": {}}
    return data if isinstance(data, dict) else {"mcpServers": {}}


def _standard_bot_server(project_root: Path) -> dict[str, Any]:
    return {
        "command": "bash",
        "args": [str(project_root / "mcp-servers" / "bot" / "start.sh")],
        "env": {"PROJECT_DIR": str(project_root)},
    }


def _project_root_from_base(base_path: Path | None, project_root: str | Path | None) -> Path:
    if project_root is not None:
        return Path(project_root)
    if base_path is not None and base_path.name.startswith(".mcp"):
        return base_path.parent
    return Path.cwd()


def _resolve_base_config(
    base_mcp_config: str | Path | None,
    runtime_path: Path,
    project_root: str | Path | None,
) -> Path | None:
    if base_mcp_config:
        base_path = Path(base_mcp_config)
        if base_path.exists():
            return base_path
    default_path = default_bot_mcp_config(_project_root_from_base(None, project_root))
    if default_path.exists():
        return default_path
    if runtime_path.exists():
        return runtime_path
    return None


def ensure_bot_runtime_mcp_config(
    *,
    base_mcp_config: str | Path | None,
    channel_key: ChannelKey,
    runtime_path: Path,
    project_root: str | Path | None = None,
) -> str:
    """Write a bot-session MCP config with current Telegram routing env.

    The generated file is intentionally recoverable: callers can recreate it
    from ``channel_key`` and the base config before every tmux respawn.
    """
    base_path = _resolve_base_config(base_mcp_config, runtime_path, project_root)
    root = _project_root_from_base(base_path, project_root)
    data = _load_mcp_config(base_path) if base_path is not None else {"mcpServers": {}}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    raw_bot = servers.get("bot")
    bot_server = dict(raw_bot) if isinstance(raw_bot, dict) else _standard_bot_server(root)
    raw_env = bot_server.get("env")
    env = dict(raw_env) if isinstance(raw_env, dict) else {}
    env.setdefault("PROJECT_DIR", str(root))
    env["TELEGRAM_CHAT_ID"] = str(channel_key[0])
    env["TELEGRAM_THREAD_ID"] = "" if channel_key[1] is None else str(channel_key[1])
    env["TELEGRAM_CONTEXT_LOCK"] = "1"
    bot_server["env"] = env
    servers["bot"] = bot_server

    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_path.parent, _RUNTIME_DIR_MODE)
    tmp_path = runtime_path.with_suffix(runtime_path.suffix + ".tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
        os.chmod(tmp_path, _RUNTIME_FILE_MODE)
        os.replace(tmp_path, runtime_path)
        os.chmod(runtime_path, _RUNTIME_FILE_MODE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
    return str(runtime_path)
