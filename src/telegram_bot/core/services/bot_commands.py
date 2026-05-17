"""Telegram bot command menu registration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeUnion,
)


class BotCommandSetter(Protocol):
    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: BotCommandScopeUnion | None = None,
        language_code: str | None = None,
        request_timeout: int | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class LocalizedBotCommand:
    command: str
    ru_description: str
    en_description: str


PUBLIC_BOT_COMMANDS: tuple[LocalizedBotCommand, ...] = (
    LocalizedBotCommand("start", "Запустить бота", "Start the bot"),
    LocalizedBotCommand("clear", "Сбросить текущий чат", "Reset the current chat"),
    LocalizedBotCommand("cancel", "Отменить текущую обработку", "Cancel current processing"),
    LocalizedBotCommand("language", "Сменить язык интерфейса", "Change interface language"),
    LocalizedBotCommand("mode", "Выбрать режим выполнения", "Choose execution mode"),
    LocalizedBotCommand("stream", "Выбрать режим ответов", "Choose response mode"),
    LocalizedBotCommand("engine", "Выбрать Claude Code или Codex", "Choose Claude Code or Codex"),
    LocalizedBotCommand("resume", "Возобновить сохраненную сессию", "Resume a saved session"),
    LocalizedBotCommand("kill", "Остановить tmux-сессию", "Stop the tmux session"),
    LocalizedBotCommand("recycle", "Перезапустить runtime топика", "Restart topic runtime"),
    LocalizedBotCommand("mcpstatus", "Показать MCP-процессы", "Show MCP processes"),
    LocalizedBotCommand("tui", "Открыть панель TUI", "Open the TUI panel"),
    LocalizedBotCommand(
        "tail",
        "Открыть панель TUI (старый алиас)",
        "Open the TUI panel (legacy alias)",
    ),
)


def build_bot_commands(
    language_code: str,
    *,
    extra_commands: Sequence[LocalizedBotCommand] = (),
) -> list[BotCommand]:
    """Build Telegram Bot API commands for one language."""
    if language_code not in {"ru", "en"}:
        raise ValueError(f"Unsupported bot command language: {language_code}")

    commands = (*PUBLIC_BOT_COMMANDS, *extra_commands)
    return [
        BotCommand(
            command=command.command,
            description=(
                command.ru_description if language_code == "ru" else command.en_description
            ),
        )
        for command in commands
    ]


async def setup_bot_commands(
    bot: BotCommandSetter,
    *,
    extra_commands: Sequence[LocalizedBotCommand] = (),
) -> None:
    """Register the same command menu in every scope a client may read.

    Telegram resolves the command list per chat by walking scopes from most
    specific (chat → all_chat_administrators → all_group_chats / all_private_chats)
    down to default and returning the first non-empty list — it does not merge
    them. Writing only to default leaves any pre-existing list in a narrower
    scope shadowing the new commands. Writing the full list everywhere keeps
    the menu consistent regardless of past state.
    """
    ru_commands = build_bot_commands("ru", extra_commands=extra_commands)
    en_commands = build_bot_commands("en", extra_commands=extra_commands)

    scopes: tuple[BotCommandScopeUnion | None, ...] = (
        None,
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    )

    for scope in scopes:
        await bot.set_my_commands(ru_commands, scope=scope)
        await bot.set_my_commands(ru_commands, scope=scope, language_code="ru")
        await bot.set_my_commands(en_commands, scope=scope, language_code="en")
