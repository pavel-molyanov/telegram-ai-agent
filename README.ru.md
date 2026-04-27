# Telegram AI Agent

[English version](README.md)

Telegram AI Agent - open-source Telegram-бот для управления Claude Code и Codex
CLI на вашем VPS. Он превращает Telegram в удаленный интерфейс для вайбкодинга:
создаете топик под проект, пишете задачи с телефона, прикладываете файлы или
голосовые, смотрите прогресс, возвращаетесь к старым сессиям и управляете живой
терминальной TUI, когда агенту нужен ввод.

В репозитории лежит только публичный переиспользуемый runtime бота. Здесь нет
приватных данных ассистента, приватных промптов, runtime state, реальных ID,
токенов или машинно-специфичного деплоя.

## Что Можно Делать

- Запускать Claude Code или Codex из Telegram private chats и group forum
  topics.
- Держать отдельный Telegram-топик под каждый проект, workflow или долгий
  контекст агента.
- Привязать топик к папке на VPS, например `/home/user/projects/my-app`.
- Выбирать Claude Code или Codex отдельно для каждого топика.
- Использовать постоянную `tmux`-сессию для полноценной разработки или короткий
  subprocess для простых разовых задач.
- Отправлять текст, фото, документы, пачки forwarded messages и, опционально,
  voice messages.
- Делать свои prompt modes под разные сценарии.
- Открывать live TUI snapshot через `/tui` и нажимать кнопки Enter, Esc,
  стрелки, цифры, refresh и close.
- Возобновлять старые сессии через reply на сообщения бота или через `/resume`.
- Давать агенту отправлять сообщения, картинки и документы обратно в Telegram
  через встроенный bot MCP server.

## Как Это Устроено

Бот запускается на той же машине, где стоят Claude Code и/или Codex CLI.
Telegram - только интерфейс управления. Когда вы отправляете сообщение, бот:

1. проверяет, что ваш Telegram user ID разрешен;
2. скачивает приложенные медиа, если нужно;
3. находит настройки текущего чата или forum topic;
4. отправляет prompt в Claude Code или Codex в нужной рабочей папке;
5. стримит прогресс и финальный ответ обратно в Telegram.

Forum topics изолированы по Telegram `chat_id` и `thread_id`. У каждого топика
может быть свой:

- `cwd`: папка проекта на VPS;
- `mode`: prompt mode;
- `engine`: `claude` или `codex`;
- `exec_mode`: `tmux` или `subprocess`;
- `stream_mode`: `verbose`, `live` или `minimal`;
- `mcp_config`: опциональный MCP config для агента;
- `model`: опциональный override модели.

## Требования

Нужен Linux-сервер или VPS, где будут работать бот и agent CLIs.

- Python 3.12+
- `uv`
- Telegram bot token из `@BotFather`
- ваш числовой Telegram user ID из `@userinfobot`
- Claude Code CLI и/или Codex CLI, установленные под тем же Linux-user, который
  запускает бота
- `tmux` для постоянных dev-сессий
- опционально: Deepgram API key для voice transcription

Бот может работать, если установлен только один agent CLI. По умолчанию он
предпочитает Claude Code, но если Claude Code нет, а Codex установлен, топик
может работать через Codex.

Проверьте server user перед продолжением:

```bash
python3 --version
uv --version
command -v tmux
command -v claude || true
command -v codex || true
```

Должен существовать хотя бы один из `claude` или `codex`. Также убедитесь, что
CLI залогинен или настроен под тем же Linux-user, который будет запускать бота.

## Вариант Настройки A: Через Агента

Если на VPS уже есть Claude Code или Codex, это самый простой путь.

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
```

Откройте Claude Code или Codex в этом репозитории и попросите:

```text
Настрой этот Telegram bot через bot-setup skill.
```

В репозитории есть setup skills для обоих runtime:

- `.claude/skills/bot-setup/SKILL.md`
- `.codex/skills/bot-setup/SKILL.md`

Для настройки forum topics попросите:

```text
Создай и настрой Telegram forum topics через topic-setup skill.
```

Topic setup skills лежат здесь:

- `.claude/skills/topic-setup/SKILL.md`
- `.codex/skills/topic-setup/SKILL.md`

Агент должен спросить язык UI, default engine, execution mode и нужен ли
systemd service.

## Вариант Настройки B: Руками

Склонируйте репозиторий и установите зависимости:

```bash
git clone https://github.com/pavel-molyanov/telegram-ai-agent.git
cd telegram-ai-agent
uv sync
cp .env.example .env
cp topic_config.example.json topic_config.json
chmod 600 .env topic_config.json
```

Отредактируйте `.env`:

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
CC_MAX_TURNS=100
CC_INACTIVITY_KILL_SEC=1800
```

Пояснения:

- `TELEGRAM_BOT_TOKEN`: создайте бота в `@BotFather`.
- `ALLOWED_USER_IDS`: JSON array Telegram user IDs, которым можно пользоваться
  ботом.
- `BOT_LANG`: `en` или `ru`. После смены языка перезапустите бота.
- `DEFAULT_CWD`: рабочая папка по умолчанию для ненастроенных топиков.
- `DEEPGRAM_API_KEY`: оставьте пустым, если не нужны voice messages.

Запустите в foreground:

```bash
uv run telegram-bot
```

Откройте Telegram и отправьте `/start`. Перед установкой systemd отправьте одно
обычное сообщение и проверьте, что Claude Code или Codex отвечает. Так проще
поймать missing CLI auth, неправильный `PATH` и плохие project paths, пока логи
видны прямо в терминале.

## Настройка Telegram-Бота И Группы

Private chat подходит для простого использования. Для реальной проектной работы
удобнее Telegram supergroup с forum topics.

1. В `@BotFather` создайте бота и скопируйте token в `.env`.
2. В `@BotFather` используйте `/setprivacy` и отключите privacy, если хотите,
   чтобы бот получал обычные non-command messages в group topics.
3. Создайте Telegram group или supergroup.
4. Включите forum topics в настройках группы.
5. Добавьте бота в группу.
6. Сделайте бота админом с правами читать сообщения, отправлять сообщения,
   управлять topics и отправлять media/documents.
7. Создайте topics вручную или используйте `topic-setup` skill из Claude
   Code/Codex.

Каждый topic - отдельное рабочее пространство агента. Можно сделать один topic
для product repo, другой для лендинга, третий для задач, четвертый для постов и
так далее. В публичной версии есть общий prompt `free` и заменяемый пример
`task`. В своей установке можно добавить любые prompt modes под свои workflows.

Когда появляется новый forum topic, запущенный бот регистрирует его в
`topic_config.json` с настройками по умолчанию. Topic сразу принимает сообщения,
но работает на defaults, пока вы его не настроите.

Публичный `topic_config.json` хранит topics по Telegram `message_thread_id`,
поэтому самый простой и рекомендуемый setup - одна forum group на один bot
config. Чтобы узнать topic ID, запустите бота, создайте или переименуйте topic,
отправьте туда сообщение, затем откройте сгенерированный `topic_config.json` и
отредактируйте новую запись.

## Topic Configuration

Можно редактировать `topic_config.json` напрямую, использовать `/engine`,
`/mode` и `/stream` внутри Telegram forum topics или попросить `topic-setup`
skill настроить topics.

Термины важны: поле config `mode` означает prompt mode. Telegram-команда
`/mode` меняет execution mode, который хранится как `exec_mode`.

Пример:

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

Поля:

- `name`: понятное название.
- `type`: `assistant` или `project`.
- `mode`: prompt mode. Публичные modes - `free` и `task`.
- `cwd`: absolute path к проекту или `null`, чтобы использовать `DEFAULT_CWD`.
- `mcp_config`: absolute path к MCP config или `null` для bot-generated MCP
  config.
- `stream_mode`: `verbose`, `live` или `minimal`.
- `exec_mode`: `tmux` или `subprocess`.
- `engine`: `claude` или `codex`.
- `model`: опциональный override модели или `null`.

Для `cwd` и `mcp_config` используйте absolute paths. Ставьте `mcp_config` в
`null`, если у вас еще нет настоящего MCP config file для проекта. Не коммитьте
настоящий `topic_config.json`.

## Prompt Modes

Prompt files лежат в `src/telegram_bot/prompts/`.

Публичные modes:

- `free`: дефолтный общий/project prompt, файл `default.md`.
- `task`: маленький заменяемый пример task-management prompt, файл
  `task-manager.md`.

Для no-code customization отредактируйте или замените `task-manager.md` и
используйте `"mode": "task"` в нужных topics. Если хотите новое имя mode,
например `blog`, добавьте prompt file и расширьте runtime tool mapping для
этого mode в коде; иначе у агента может не быть allowed tool list. Не кладите в
public repo приватные данные, секреты, личные workflows и реальный customer
context.

## Execution Modes

`/mode` выбирает, как запускается agent process.

### tmux

`tmux` нужен для полноценной разработки.

Бот поднимает постоянную terminal session и отправляет ваши Telegram-сообщения
напрямую в Claude Code или Codex TUI. Сессия хранит контекст, может переживать
рестарты бота, и ее можно смотреть через `/tui`.

Используйте это, когда:

- вы редактируете codebase;
- агенту нужен длинный контекст;
- агент может показывать permission questions или interactive menus;
- нужны `/resume` и reply-to-session behavior.

`tmux` потребляет ресурсы, пока сессия жива. Когда она больше не нужна,
используйте `/kill`.

### subprocess

`subprocess` нужен для коротких задач.

Каждое сообщение запускает свежий CLI process, получает ответ и завершает его.
Это удобно для простых вопросов, заметок, маленьких преобразований и задач, где
не хочется держать постоянную TUI-сессию в фоне.

Private chats используют настройки по умолчанию и подходят для простого
использования. Per-topic controls вроде `/mode`, `/stream`, `/engine` и
`/resume` работают в forum topics, потому что им нужна topic-specific config
entry.

## Stream Modes

`/stream` управляет тем, сколько прогресса бот отправляет в Telegram.

- `verbose`: подробный прогресс отдельными сообщениями. Полезно для debugging.
- `live`: один редактируемый progress message плюс финальный ответ. Лучший
  default для большинства проектных задач.
- `minimal`: в основном финальные ответы. Хорошо, когда нужен тихий чат.

## TUI Mode

`/tui` открывает snapshot живой tmux pane и добавляет кнопки управления.

Это нужно, потому что Claude Code и Codex CLI - терминальные приложения. Они
могут показывать permission prompts, menus, confirmations, model pickers и
другой interactive UI. Бот умеет писать в терминал и читать transcript, но
иногда нужно увидеть и порулить TUI напрямую.

Используйте `/tui`, чтобы:

- посмотреть, что сейчас делает агент;
- нажать Enter, Esc, arrows, Tab, Backspace, Ctrl+C или цифры;
- обработать permission dialogs;
- выйти из состояния, которое выглядит как зависшая TUI.

`/tail` - legacy alias для `/tui`.

## Reply, Resume И Sessions

Бот запоминает, какая agent session породила каждый ответ. Если вы отвечаете
reply на старое сообщение бота, он может направить новое сообщение обратно в
соответствующую сессию. Это удобно, когда в одном Telegram topic есть несколько
исторических сессий.

В tmux mode команда `/resume` показывает сохраненные sessions для рабочей папки
топика и позволяет переключиться обратно в одну из них. Если target session
относится к другому engine или execution mode, бот может переключить настройки
топика перед resume. Если live tmux session нужно заменить, она может быть
остановлена в рамках такого switch.

Slash commands устроены отдельно: в tmux topics non-bot commands вроде `/model`
или `/compact` отправляются в live TUI, а не в historical session из reply.

`/clear` начинает свежий logical context для текущего topic. В tmux mode бот
сбрасывает или пересоздает tmux session в зависимости от текущего состояния.
`/new` все еще существует как legacy alias, но в меню показывается `/clear`.

## Команды

- `/start`: проверить, что бот отвечает, и показать базовую клавиатуру.
- `/clear`: сбросить сессию текущего topic.
- `/cancel`: отменить текущую обработку.
- `/language`: показать или сменить язык UI, например `/language ru`.
- `/mode`: только forum topics; выбрать `tmux` или `subprocess`. При
  переключении с `tmux` на `subprocess` активная tmux session останавливается.
- `/engine`: только forum topics; выбрать Claude Code или Codex. Смена engine
  сбрасывает активную session.
- `/stream`: только forum topics; выбрать `verbose`, `live` или `minimal`.
- `/resume`: только forum topics; возобновить сохраненную tmux session для cwd
  текущего topic.
- `/tui`: показать и управлять живой tmux TUI.
- `/tail`: legacy alias для `/tui`.
- `/kill`: остановить активную tmux session и освободить ресурсы.

Рекомендации:

- Настоящая разработка: `/mode` -> `tmux`, `/stream` -> `live`.
- Короткие разовые задачи: `/mode` -> `subprocess`, `/stream` -> `minimal` или
  `live`.
- Старая session больше не нужна: `/kill`.

## MCP Bot Server

Встроенный MCP server позволяет агенту отправлять контент обратно в текущий
Telegram topic. Сессии, запущенные ботом, автоматически получают topic-scoped
MCP config.

Публичные prompt modes разрешают generic bot tools:

- `send_message`
- `send_image`
- `send_document`

Не коммитьте реальные `.mcp.json`: там могут быть токены или локальные пути.

Если `mcp_config` равен `null`, бот генерирует runtime MCP config с Telegram bot
server и routing текущего topic. Если `mcp_config` указывает на существующий
файл, бот использует его как base config и добавляет Telegram bot server в
runtime copy. Если path не существует, используйте `null` или исправьте path,
прежде чем полагаться на project-specific MCP tools.

## Автозапуск Через Systemd

На VPS лучше запускать бота как systemd service, чтобы он стартовал после
reboot и поднимался обратно после падений.

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

У systemd может быть более короткий `PATH`, чем в интерактивной shell. Перед
боевым запуском проверьте, что service user видит `uv`, `tmux` и хотя бы один
из `claude` или `codex`. Если CLIs лежат в user-local directory, добавьте в unit
строку `Environment=PATH=...` или используйте absolute paths.

Если systemd перестал пробовать после частых падений:

```bash
sudo systemctl reset-failed telegram-bot
sudo systemctl start telegram-bot
```

## Runtime Files

Не коммитьте runtime files:

- `.env`
- `topic_config.json`
- `session_mapping.json`
- `channel_sessions.json`
- `tmux_sessions/`
- `data/`
- `.mcp*.json`
- `.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`
- `__pycache__/`, `*.pyc`, `*.pyo`

Коммитьте только public-safe examples и docs: `.env.example`,
`topic_config.example.json`, README files.

## Security Model

Бот рассчитан на trusted personal или small-team use. `ALLOWED_USER_IDS` -
главный access control. Любой, кто может писать боту, может попросить
настроенный local agent CLI работать в заданной рабочей папке, включая file
edits и shell/tool actions, разрешенные этим CLI.

Не открывайте бота untrusted users. Не настраивайте project topics на папки,
которые агенту нельзя читать или менять. Лучше запускать бота под отдельным
low-privilege Linux user. Если bot token утек, перевыпустите его в
`@BotFather`.

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

Issues, bug reports и идеи welcome. Откройте GitHub issue, если что-то
непонятно, сломано или не хватает важной возможности.

Бота сделал Паша Молянов. Я пишу про бизнес, AI-ассистентов, разработку и
запуск полезных сервисов в Telegram-канале:
[@molyanov_blog](https://t.me/+zJ5qmSsoYediYzdi).

## License

MIT
