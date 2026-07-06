---
description: Switch the tracked task mid-session (closes the previous task range)
---

Switch the active task for this session by running:

```
python3 ~/.qwen/skills/qwen-usage-tracker/task_layer.py switch {{args}}
```

Run that command via your bash tool (passing `{{args}}` as the task key, e.g. `TASK-123`), then confirm the switch happened: which task was closed and which is now active.
