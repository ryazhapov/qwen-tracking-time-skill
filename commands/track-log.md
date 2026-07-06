---
description: Show time breakdown for the current session and prepare to log it
---

Show the time/token breakdown for the current session by running:

```
python3 ~/.qwen/skills/time-tracker/task_layer.py log
```

Run that command via your bash tool, then report the breakdown clearly: per-task focus time, API time, and tokens. If there is unattributed time, flag it and suggest how to resolve it (`map` a branch, or mark `untracked`). If everything is attributed, ask whether to submit the time entries to the task tracker.
