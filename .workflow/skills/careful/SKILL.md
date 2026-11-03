---
name: careful
description: Защитный режим для опасных команд — перед rm -r, DROP/TRUNCATE, git push --force, git reset --hard, kubectl delete, docker prune и похожими спрашивает подтверждение; удаление корня или домашнего каталога и force-push в основную ветку запрещает. Вызывай перед работой со стейджингом, общими базами, живыми системами или когда человек говорит «осторожно», «режим безопасности», «be careful». Действует до конца сессии.
allowed-tools:
  - Bash
  - Read
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: 'python3 "$HOME/.claude/skills/careful/bin/check.py"'
          statusMessage: "careful: проверяю команду"
---

# /careful — защита от опасных команд

Режим включён до конца сессии. Каждая команда Bash перед запуском проверяется хуком `bin/check.py`.

| Что | Пример | Решение |
|---|---|---|
| рекурсивное удаление | `rm -rf data/` | спросить |
| удаление `/`, `~`, `$HOME` | `rm -rf ~` | **запрет** |
| `DROP`, `TRUNCATE`, `DELETE` без `WHERE` | `DROP TABLE users;` | спросить |
| force-push | `git push -f origin feature/x` | спросить |
| force-push в основную ветку | `git push -f origin main` | **запрет** |
| `git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`, `git branch -D` | | спросить |
| `kubectl delete`, `docker rm -f`, `docker system/volume/image prune` | | спросить |
| `mkfs`, `dd of=/dev/…`, `chmod -R 777` | | спросить |

Без вопроса удаляются каталоги сборки: `node_modules`, `dist`, `build`, `__pycache__`, `.cache`, `.venv`, `target`, `coverage` и похожие. `--force-with-lease` не считается force-push.

Свои шаблоны проекта — в `docs/rules/careful-patterns.txt`, одно регулярное выражение на строку, `#` — комментарий. Они только добавляют вопросы; отключить встроенные нельзя.

Это подстраховка поверх правил, а не граница безопасности: команду, собранную во время выполнения, строковая проверка не увидит. Жёсткие запреты — в `permissions.deny` файла `.claude/settings.json`. Выключить режим — начать новую сессию.

Источник идеи и списка шаблонов — gstack `/careful` (MIT), см. `NOTICE.md` в корне workflow.
