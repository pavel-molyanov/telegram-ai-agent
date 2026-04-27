# Telegram AI Agent

Open-source Telegram-бот для запуска Claude Code или Codex из Telegram:
private chats и forum topics. У каждой форум-темы может быть свой рабочий
каталог, prompt mode, engine, execution mode, stream mode, model и MCP config.

В репозитории лежит только переиспользуемый runtime бота. Здесь нет приватных
данных ассистента, приватных промптов, runtime state, секретов или
машинно-специфичного деплоя.

## Как Это Работает

Бот получает сообщения из Telegram, проверяет `ALLOWED_USER_IDS`, при
необходимости скачивает медиа и отправляет запрос в Claude Code или Codex на
той же машине.

Есть две независимые настройки runtime:

- `engine`: `claude` или `codex`.
- `exec_mode`: `subprocess` для разовых запусков или `tmux` для постоянной TUI
  сессии, которая переживает рестарт бота.

Forum topics изолируются по Telegram `chat_id` и `thread_id`. Бот читает
`topic_config.json` и понимает, в каком каталоге работать, какой prompt mode
использовать, какой engine запустить, какой MCP config подключить и как
показывать прогресс.

## Возможности

- Private chats и supergroup forum topics.
- Claude Code и Codex.
- Обычный subprocess mode и persistent tmux mode.
- Stream modes: `verbose`, `live`, `minimal`.
- TUI snapshot и управление через `/tui`.
- Resume сессий в tmux mode.
- Текст, фото, документы, пачки forwarded messages и voice transcription.
- Topic-scoped `topic_config.json`.
- Bot MCP server для отправки сообщений и файлов обратно в Telegram.
- Английский и русский UI через `BOT_LANG`.
- systemd autostart с restart-on-failure.

## Требования

- Python 3.12+
- `uv`
- Telegram bot token от `@BotFather`
- Твой Telegram user ID из `@userinfobot`
- Claude Code и/или Codex CLI на машине, где работает бот. Бот умеет работать,
  если установлен только один из них: по умолчанию предпочитает Claude Code,
  но переключает топик на Codex, если Claude Code отсутствует, а Codex есть.
- Опционально: `tmux` для persistent TUI mode
- Опционально: Deepgram API key для voice transcription

## Настройка Через Агента

Если ты используешь Claude Code или Codex внутри этого репозитория, попроси:

```text
Настрой этот Telegram bot по bot-setup skill.
```

Скиллы есть для обоих runtime:

- `.claude/skills/bot-setup/SKILL.md`
- `.codex/skills/bot-setup/SKILL.md`

Настройка forum topics описана здесь:

- `.claude/skills/topic-setup/SKILL.md`
- `.codex/skills/topic-setup/SKILL.md`

Агент должен спросить язык UI (`en` или `ru`), какой engine использовать по
умолчанию, и нужен ли systemd service для автозапуска.

## Быстрый Старт

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
cp .env.example .env
cp topic_config.example.json topic_config.json
```

Отредактируй `.env`:

```env
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
ALLOWED_USER_IDS=[123456789]
BOT_LANG=ru
DEEPGRAM_API_KEY=
PROJECT_ROOT=.
DEFAULT_CWD=.
FILE_CACHE_DIR=./data
TOPIC_CONFIG_PATH=./topic_config.json
TMUX_SESSIONS_DIR=./tmux_sessions
```

Для английского UI поставь `BOT_LANG=en`. После смены языка перезапусти бота.
Для голосовых сообщений зарегистрируйся в Deepgram, создай API key, запиши его
в `DEEPGRAM_API_KEY` и перезапусти бота. Пустое значение отключает voice
transcription.

Локальный запуск:

```bash
uv run telegram-bot
```

Открой Telegram, отправь `/start`, потом напиши сообщение боту.

## Настройка Telegram Forum

Private chat подходит для простого использования. Для проектной работы удобнее
Telegram supergroup с forum topics:

1. Создай бота через `@BotFather` и запиши token в `.env`.
2. Создай Telegram group или supergroup и включи forum topics в настройках.
3. Добавь бота в группу админом с правами читать сообщения, отправлять
   сообщения, управлять topics и отправлять media/documents.
4. Создавай topics руками в Telegram или попроси локального Claude Code/Codex
   агента использовать `topic-setup` skill.

Когда topic создан, запущенный бот автоматически регистрирует его в
`topic_config.json` с generic defaults. Topic сразу принимает сообщения, но
использует дефолтные настройки, пока его не настроили.

Новые topics предпочитают engine `claude`. Если Claude Code отсутствует, но
Codex установлен, бот стартует с Codex и сохраняет `engine=codex` для этого
topic. Если не установлен ни Claude Code, ни Codex, бот всё равно запускается
и просит установить один из CLI.

## Автозапуск Через Systemd

На сервере лучше запускать бота через systemd: он стартует после перезагрузки
машины и поднимается обратно, если процесс упал.

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

Логи:

```bash
journalctl -u telegram-bot -f
```

В service-файле стоит `Restart=on-failure` и `RestartSec=5`, поэтому после
падения бот поднимается обратно через пять секунд. Если он падает чаще пяти раз
за минуту, systemd перестанет пытаться. После исправления причины:

```bash
sudo systemctl reset-failed telegram-bot
sudo systemctl start telegram-bot
```

## Forum Topics

Для supergroup forums настрой каждую тему в `topic_config.json`. Можно
редактировать файл напрямую, использовать `/engine`, `/mode` и `/stream` в
Telegram или попросить локального агента запустить `topic-setup` skill:

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

Для `cwd` и `mcp_config` используй absolute paths. Если хочешь использовать
автоматически созданный bot MCP config, поставь `mcp_config: null`.

Важные поля:

- `type`: `assistant` или `project`.
- `mode`: `free` - стандартный project/general prompt. `task` добавлен как
  пример второго prompt mode; его можно заменить своим prompt-файлом и указать
  этот mode в нужной теме.
- `stream_mode`: `verbose`, `live`, `minimal`.
- `exec_mode`: `subprocess` для разовых ассистентских задач, где не нужно
  постоянно держать запущенный agent process, или `tmux` для полноценной
  разработки с persistent context, TUI snapshot, `/resume` и `/tui`.
- `engine`: `claude` или `codex`.
- `model`: опциональный model override или `null`.

Бот автоматически регистрирует созданные руками Telegram topics, но не угадывает
правильный project path. Используй `topic-setup`, когда topic нужно привязать к
конкретному project directory, MCP config, engine, execution mode, stream mode
или model.

## Команды

- `/start` - показать базовую клавиатуру и проверить, что бот отвечает.
- `/new` - сбросить сессию текущей темы. В tmux mode сам tmux-процесс остаётся
  жив, сбрасывается логический контекст.
- `/clear` - alias для `/new`.
- `/cancel` - отменить текущую обработку. Используй перед сменой настроек, если
  задача ещё выполняется.
- `/mode` - выбрать транспорт выполнения: regular subprocess для разовых задач
  или tmux для постоянной dev-сессии.
- `/engine` - выбрать Claude Code или Codex для текущей forum topic.
- `/stream` - выбрать доставку прогресса: `verbose`, `live`, `minimal`.
- `/resume` - возобновить сохранённую tmux-сессию для cwd текущей темы.
- `/tui` - показать snapshot tmux TUI и кнопки управления.
- `/tail` - alias для `/tui`.
- `/kill` - остановить активную tmux-сессию и освободить ресурсы.

Рекомендации:

- Разовые заметки и короткие задачи: `/mode` -> regular, `/stream` -> minimal
  или live.
- Настоящая разработка: `/mode` -> tmux, `/stream` -> live.
- Старая tmux-сессия больше не нужна: `/kill`.

## Prompt Modes

Prompt-файлы лежат в `src/telegram_bot/prompts/`:

- `default.md`
- `task-manager.md`

`default.md` - стандартный project/general prompt для `free`. `task-manager.md`
- заменяемый пример второго режима, который показывает, как может работать
дополнительный mode. Для своего workflow добавь public-safe prompt-файл и
поставь в теме `mode`, равный имени файла без `.md`.

Держи public prompts generic. Не коммить приватные данные, приватные workflows
или секреты.

## MCP Bot Server

Bot MCP server позволяет агенту отправлять сообщения и файлы обратно в текущую
Telegram-тему. Runtime автоматически создаёт topic-scoped MCP configs для
сессий, запущенных ботом.

Оба публичных prompt modes, `free` и `task`, имеют одинаковый generic bot MCP
whitelist для отправки обратно в Telegram: `send_message`, `send_image`,
`send_document`.

Реальные `.mcp.json` не коммить: там могут быть токены или локальные пути.

## Runtime Files

Никогда не коммить:

- `.env`
- `topic_config.json`
- `session_mapping.json`
- `channel_sessions.json`
- `tmux_sessions/`
- `data/`
- `.mcp*.json`
- `.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`
- `__pycache__/`, `*.pyc`, `*.pyo`

Коммитить можно только примеры вроде `.env.example`,
`topic_config.example.json` и public-safe docs.

## Разработка

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

Бот рассчитан на trusted personal или small-team use. `ALLOWED_USER_IDS` -
основной access control. Любой пользователь с доступом к боту может попросить
локальный agent CLI работать в настроенном рабочем каталоге.

Не открывай бота untrusted users. Не настраивай project topics на каталоги,
которые агенту нельзя читать или менять.

## Автор

Бота сделал Паша Молянов. Если бот оказался полезен, будет здорово, если
подпишешься на мой Telegram-канал про бизнес, AI-ассистентов, разработку и
запуск полезных сервисов: [@molyanov_blog](https://t.me/+zJ5qmSsoYediYzdi).

## Лицензия

MIT
