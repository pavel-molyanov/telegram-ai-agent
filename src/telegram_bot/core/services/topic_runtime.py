"""Pure runtime config resolution for topic-scoped agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.cc_modes import Mode
from telegram_bot.core.services.topic_config import Engine, ExecMode, StreamMode, TopicSettings


@dataclass(frozen=True)
class BotDefaults:
    cwd: Path
    mcp_config: Path
    mode: Mode = "free"
    engine: Engine = "claude"
    model: str | None = None
    exec_mode: ExecMode = "subprocess"
    stream_mode: StreamMode = "live"


@dataclass(frozen=True)
class TopicRuntimeConfig:
    cwd: Path
    mode: Mode
    mcp_config: str | None
    engine: Engine
    model: str | None
    exec_mode: ExecMode
    stream_mode: StreamMode


def resolve_topic_runtime_config(
    settings: TopicSettings,
    defaults: BotDefaults,
) -> TopicRuntimeConfig:
    """Resolve nullable topic settings into concrete runtime values."""
    cwd = Path(settings.cwd) if settings.cwd is not None else defaults.cwd
    mcp_config = (
        settings.mcp_config if settings.mcp_config is not None else str(defaults.mcp_config)
    )
    mode = settings.mode if settings.mode in {"task", "free"} else defaults.mode
    return TopicRuntimeConfig(
        cwd=cwd,
        mode=mode,  # type: ignore[arg-type]
        mcp_config=mcp_config,
        engine=settings.engine or defaults.engine,
        model=settings.model if settings.model is not None else defaults.model,
        exec_mode=settings.exec_mode or defaults.exec_mode,
        stream_mode=settings.stream_mode or defaults.stream_mode,
    )
