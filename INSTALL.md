# Установка time-tracker

Скилл для трекинга времени и токенов в Qwen Code. Парсит логи qwen-code, атрибутирует время по задачам, готовит time entries для списания в трекер.

## Требования

| Что | Версия | Зачем | Проверка |
|---|---|---|---|
| **Python 3** | ≥ 3.8 | Все скрипты скилла | `python3 --version` |
| **qwen-code** | ≥ 0.19 | Хуки, custom commands | `qwen --version` |
| **bash** | любая | Хуки SessionStart/SessionEnd | `bash --version` |
| **standard Unix utils** | — | `find`, `grep`, `wc` (в хуках) | встроены в macOS/Linux |

**Не требуется:** pip-пакеты, MCP SDK, внешние трекеры, ActivityWatch, tmux. Скилл полностью самодостаточен на стандартном Python.

**Опционально для multi-host:** `rsync` + SSH-доступ (для подтягивания логов с удалённых серверов, см. `scripts/sync-remote.sh`).

**Опционально для списания в трекер:** MCP-сервер (не реализован, см. `MCP_SERVER_SPEC.md`).

## Установка

### 1. Скопировать скилл

Скилл должен лежать в `~/.qwen/skills/time-tracker/`:

```
~/.qwen/skills/time-tracker/
├── SKILL.md
├── qwen_usage.py
├── task_layer.py
├── config.json
├── branch_map.json         (создастся при первом `map`)
├── task_assignments.jsonl  (создастся при первом `assign`/`switch`)
├── pending.jsonl           (создастся при первом `/quit`)
├── MCP_SERVER_SPEC.md
├── LIMITATIONS.md
├── INSTALL.md              (этот файл)
├── hooks/
│   ├── on_session_start.sh
│   ├── on_session_end.sh
│   └── check.sh
└── scripts/
    └── sync-remote.sh
```

### 2. Сделать хуки исполняемыми

```bash
chmod +x ~/.qwen/skills/time-tracker/hooks/*.sh
```

Это **обязательный шаг** — без executable-бита хуки не запустятся. Если что-то не работает, проверьте:

```bash
ls -la ~/.qwen/skills/time-tracker/hooks/
# должно быть -rwxr-xr-x для всех .sh
```

### 3. Зарегистрировать хуки в settings.json

Добавить в `~/.qwen/settings.json` блок `hooks` (на верхнем уровне, рядом с `model`, `security` и т.д.):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/ВАШ_ЛОГИН/.qwen/skills/time-tracker/hooks/on_session_start.sh",
            "name": "usage-tracker-pending-check",
            "timeout": 5000
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "prompt_input_exit|clear|logout|other",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/ВАШ_ЛОГИН/.qwen/skills/time-tracker/hooks/on_session_end.sh",
            "name": "usage-tracker-stage-on-quit",
            "timeout": 10000
          }
        ]
      }
    ]
  }
}
```

**Важно:** путь должен быть **абсолютный** (`/Users/...`, не `~/...`). Замените `ВАШ_ЛОГИН` на реальный.

### 4. Создать custom commands (опционально, но рекомендуется)

Слеш-команды для удобного вызова. Создать файлы в `~/.qwen/commands/`:

- `track-status.md` — `/track-status`
- `track-pending.md` — `/track-pending`
- `track-log.md` — `/track-log`
- `track-switch.md` — `/track-switch TASK-XXX`

(Содержимое этих файлов — в репозитории скилла, в `examples/commands/`.)

### 5. Настроить config.json (опционально)

Открыть `~/.qwen/skills/time-tracker/config.json` и проверить:

| Ключ | По умолчанию | Что |
|---|---|---|
| `timezone` | `"+03:00"` (MSK) | Для полночного сплита и отображения |
| `history_horizon_days` | `30` | Сколько дней логов сканировать |
| `idle_timeout` | `300` | Секунды продолжения фокуса после последнего промпта |
| `viewing_branches` | `["master","main","release/*","develop","HEAD"]` | Ветки, не атрибутируемые через branch_map |
| `merge_short_visits_s` | `300` | Сливать заходы короче N секунд |

## Проверка установки

```bash
~/.qwen/skills/time-tracker/hooks/check.sh
```

Должно вывести `✓ All checks passed`. Если есть ошибки — следуйте инструкциям в выводе.

Дополнительно — ручной smoke-test:

```bash
python3 ~/.qwen/skills/time-tracker/task_layer.py status
python3 ~/.qwen/skills/time-tracker/task_layer.py pending
```

## Использование

Основной workflow:

1. **Стартуете qwen-code** — работаете как обычно. Логи пишутся автоматически.
2. **Хотите проверить, что накопилось** — `/track-status` или `/track-pending`.
3. **Атрибутируете время** — `map` ветки на задачу (один раз, применяется ретроактивно ко всей истории):
   ```bash
   python3 ~/.qwen/skills/time-tracker/task_layer.py map auth-rewrite TASK-123 --repo /path/to/repo
   ```
4. **Переключились на другую задачу mid-session** — `/track-switch TASK-456`.
5. **Хотите списать время** — `/track-log` показывает разбивку. Списание в трекер — через MCP (когда будет реализован) или вручную через `curl` из `task_layer.py entries`.

## Multi-host (SSH-серверы)

Если работаете и локально, и через SSH на серверах — настроить rsync-зеркало:

```bash
# crontab -e на локальной машине
*/5 * * * * ~/.qwen/skills/time-tracker/scripts/sync-remote.sh user@dev-server >/dev/null 2>&1
```

Требуется: passwordless SSH (ключ) и `rsync` на обеих машинах. Подробнее — в `SKILL.md`, секция «Multi-host».

## Обновление

После обновления файлов скилла:

1. Проверить executable-биты: `chmod +x ~/.qwen/skills/time-tracker/hooks/*.sh`.
2. Прогнать health-check: `~/.qwen/skills/time-tracker/hooks/check.sh`.
3. Если менялись пути в `settings.json` — перезапустить qwen-code.

## Удаление

```bash
# Убрать хуки из settings.json (удалить блок "hooks")
# Удалить скилл:
rm -rf ~/.qwen/skills/time-tracker
# Удалить custom commands (опционально):
rm ~/.qwen/commands/track-*.md
```

State-файлы (`task_assignments.jsonl`, `branch_map.json`, `pending.jsonl`) лежат в папке скилла и удаляются вместе с ним. Исходные логи qwen-code (`~/.qwen/usage/`, `~/.qwen/projects/`) **не трогаются** — это данные qwen-code, не скилла.

## Частые проблемы

| Симптом | Причина | Решение |
|---|---|---|
| `/track-status` → `Unknown command` | Команда не зарегистрирована | Создать файл в `~/.qwen/commands/` |
| Хук не срабатывает при старте | Не executable бит | `chmod +x hooks/*.sh` + проверить через `check.sh` |
| `permission denied` в хуке | Нет executable-бита | То же, что выше |
| Pending растёт при каждом вызове | Это норма — live-пересчёт | Данные точные на момент вызова, не дрейф |
| Промпт при старте не всплыл | Архитектурное ограничение qwen-code | `/track-pending` вручную (см. `LIMITATIONS.md` §1) |
| `status` и `pending` показывают разные числа | Был баг, исправлен | Обновить `task_layer.py`, прогнать `check.sh` |
