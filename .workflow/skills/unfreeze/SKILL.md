---
name: unfreeze
description: Снимает границу правок, заданную /freeze, для текущего проекта. Вызывай, когда человек говорит «разморозь», «сними ограничение», «unfreeze».
allowed-tools:
  - Bash
---

# /unfreeze — снять границу правок

```bash
python3 "$HOME/.claude/skills/freeze/bin/freeze.py" clear
```

Скажи человеку, что граница снята. Хук `/freeze` в этой сессии остаётся, но без границы ничего не запрещает.
