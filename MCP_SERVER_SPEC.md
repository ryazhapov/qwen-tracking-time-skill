# MCP-сервер для списания времени в таск-трекер

> **Статус: НЕ РЕАЛИЗОВАН.** Это спецификация. Сам скилл (парсинг логов, focus-model, атрибуция) полностью работает и без MCP-сервера. MCP нужен только для автоматической отправки времени в трекер (Jira/YouTrack/Linear/...).

## Что уже работает без MCP

- Парсинг логов qwen-code (`qwen_usage.py`) → focus-сегменты, токены, время.
- Атрибуция по задачам (`task_layer.py`) → per-time-range assignments, branch_map, switch/map.
- Подготовка time entries: `python3 task_layer.py entries` → JSON, готовый к отправке.
- Логи пишутся локально: `~/.qwen/usage/*.jsonl`, `~/.qwen/projects/**/*.jsonl`.
- Хуки SessionStart/SessionEnd для auto-prompt и staging.

## Что делает MCP-сервер (когда будет реализован)

Тонкий слой между `task_layer.py entries` и API трекера. Не дублирует парсинг — только HTTP.

### Инструмент 1: `log_time`

Списать время в задачу. **Идемпотентность через get-before-post**.

Вход:
```json
{
  "taskKey": "TASK-123",
  "seconds": 654,
  "date": "2026-07-04",
  "tokens": {"total": 117260, "input": 100000, "output": 17260},
  "comment": "Focus: 10m 54s | API: 1m 14s | Tokens: 117K"
}
```

Алгоритм (get-before-post):
1. `GET /rest/api/2/issue/{taskKey}/worklog` — получить worklogs задачи.
2. Искать worklog с тегом `[task_id: TASK-123]` в `comment`.
3. Если найден → `PUT /worklog/{worklogId}` (обновить).
4. Если не найден → `POST /worklog` с тегом в comment.

Тег `[task_id: TASK-XXX]` — естественный идемпотентный ключ. 10 перезапусков найдут этот тег и обновят одну запись, не плодя дубли.

### Инструмент 2: `get_task_info`

Получить информацию о задаче (когда юзер даёт ссылку в начале сессии).

Вход: `{"taskKey": "TASK-123"}` или `{"taskUrl": "https://..."}`

Выход: `{key, title, description, project, status}`

### Инструмент 3: `get_pending_time`

Сколько ещё не списано. Вызывает `task_layer.py entries` (через subprocess), возвращает pending-сегменты.

### Инструмент 4: `unsubmit`

Отменить списание (залогировали не туда). `DELETE /worklog/{trackerEntryId}` + запись в `submission_log.jsonl` со `status: "reverted"`.

## Файлы MCP-сервера (когда будет реализован)

| Файл | Назначение |
|---|---|
| `mcp_server.py` | Реализация MCP-сервера (Python, mcp SDK) |
| `submission_log.jsonl` | Аудит сабмитов (taskId, seconds, date, trackerEntryId, status) |

## Конфигурация (в `config.json`, блок `_reserved_for_mcp_server`)

```json
{
  "_reserved_for_mcp_server": {
    "auto_submit_on_quit": true,
    "sanitize_paths": true,
    "tracker": {
      "url_env": "TRACKER_API_URL",
      "token_env": "TRACKER_API_TOKEN"
    }
  }
}
```

- `auto_submit_on_quit` — авто-сабмит resolved-времени при `/quit` (через SessionEnd hook → MCP).
- `sanitize_paths` — Strip `cwd`/`gitBranch` из worklog comment (приватность путей).
- `tracker.url_env` / `token_env` — имена env-переменных с URL и токеном API.

**Важно:** эти ключи сейчас ничего не делают — они зарезервированы для MCP-сервера. Скилл без MCP работает полностью.

## LLM-флоу (когда MCP будет подключён)

**Сценарий A — юзер дал ссылку в начале:**
```
Юзер:  Буду работать над https://tracker/browse/TASK-123
Агент: get_task_info(TASK-123) → контекст задачи
       → записывает assignment (task_layer.py assign TASK-123)
[... работа ...]
В конце: get_pending_time → log_time → списано 15m в TASK-123
```

**Сценарий B — юзер забыл указать задачу:**
```
[... работа ...]
Юзер:  Кстати, это было для TASK-456
Агент: assign TASK-456 (backfill) → списает накопленное время
```

## Что нужно для реализации

1. **Python** (уже есть, нужен для скилла).
2. **MCP Python SDK** (`pip install mcp>=1.28`).
3. **httpx** (`pip install httpx`) — для HTTP-запросов к трекеру.
4. **Выбрать трекер** — Jira / YouTrack / Linear / самописный. API у них разные, MCP-сервер пишется под конкретный.
5. **Env-переменные** — `TRACKER_API_URL`, `TRACKER_API_TOKEN`.
6. **Регистрация в qwen-code:** `qwen mcp add tracker python3 ~/.qwen/skills/time-tracker/mcp_server.py`.

## Retry-очередь (опционально, для будущего)

Записи в `submission_log.jsonl` со `status: "failed"` попадают в retry. Хук `on_session_start.sh` мог бы при каждом старте пытаться их переотправить. Сейчас не реализовано — `on_session_start.sh` только показывает pending, не сабмитит.
