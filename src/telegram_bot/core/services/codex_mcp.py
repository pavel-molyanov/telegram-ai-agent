"""Safe per-topic MCP bridge for OpenAI Codex CLI.

Codex CLI does not accept Claude-style ``--mcp-config`` files. The bot passes
Codex TOML overrides that point each configured MCP server at this runner. The
runner reads the original JSON config inside the child process and execs the
real server with its env, keeping secret env values out of Codex argv.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _toml_value(value: object) -> str:
    """Return a TOML-compatible literal for simple Codex ``-c`` values."""
    return json.dumps(value, ensure_ascii=True)


def _load_config(mcp_config: str) -> dict[str, Any]:
    path = Path(mcp_config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"MCP config is not readable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP config is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("MCP config root must be an object")
    return data


def _server_items(mcp_config: str) -> list[tuple[str, dict[str, Any]]]:
    data = _load_config(mcp_config)
    servers = data.get("mcpServers")
    if servers is None:
        return []
    if not isinstance(servers, dict):
        raise ValueError("MCP config mcpServers must be an object")

    result: list[tuple[str, dict[str, Any]]] = []
    for name, server in servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid MCP server name: {name!r}")
        if not isinstance(server, dict):
            raise ValueError(f"MCP server {name!r} must be an object")
        result.append((name, server))
    return result


def build_codex_mcp_config_args(
    mcp_config: str | None,
    *,
    ignore_user_config: bool = True,
) -> list[str]:
    """Build Codex CLI args for topic-scoped MCP servers.

    Env values from the MCP JSON are intentionally omitted from argv. They are
    loaded by this module when Codex starts the server process.
    """
    args = ["--ignore-user-config"] if ignore_user_config else ["-c", "mcp_servers={}"]
    if not mcp_config:
        return args

    path = str(Path(mcp_config).resolve())
    if not Path(path).exists():
        return args
    for name, _server in _server_items(path):
        runner_args = ["-m", __name__, path, name]
        args.extend(
            [
                "-c",
                f"mcp_servers.{name}.command={_toml_value(sys.executable)}",
                "-c",
                f"mcp_servers.{name}.args={_toml_value(runner_args)}",
            ]
        )
    return args


def load_mcp_server(mcp_config: str, server_name: str) -> tuple[str, list[str], dict[str, str]]:
    """Load one MCP server entry and return ``command, argv, env`` for execvpe."""
    if not _SERVER_NAME_RE.fullmatch(server_name):
        raise ValueError(f"Invalid MCP server name: {server_name!r}")

    servers = dict(_server_items(str(Path(mcp_config).resolve())))
    server = servers.get(server_name)
    if server is None:
        raise ValueError(f"Unknown MCP server: {server_name}")

    command = server.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"MCP server {server_name!r} command must be a non-empty string")

    raw_args = server.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
        raise ValueError(f"MCP server {server_name!r} args must be a list of strings")

    raw_env = server.get("env", {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
    ):
        raise ValueError(f"MCP server {server_name!r} env must be an object of strings")

    env = dict(os.environ)
    env.update(raw_env)
    return command, [command, *raw_args], env


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m telegram_bot.core.services.codex_mcp``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: codex_mcp <mcp_config_path> <server_name>", file=sys.stderr)
        return 2
    try:
        command, exec_argv, env = load_mcp_server(argv[0], argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.execvpe(command, exec_argv, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
