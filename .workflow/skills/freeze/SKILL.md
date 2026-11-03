---
name: freeze
description: Ограничивает правки файлов одним каталогом до конца сессии — Edit, Write и NotebookEdit вне него запрещаются. Вызывай при отладке, чтобы случайно не «починить» посторонний код, при работе в одном модуле или когда человек говорит «заморозь», «правь только эту папку», «freeze». Снимается /unfreeze.
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
hooks:
  PreToolUse:
    - matcher: "Edit|Write|NotebookEdit"
      hooks:
        - type: command
          command: 'python3 "$HOME/.claude/skills/freeze/bin/freeze.py" hook'
          statusMessage: "freeze: проверяю границу правок"
---

# /freeze — правки только в одном каталоге

1. Спроси человека, каким каталогом ограничить правки, если он не назвал его сам.
2. Задай границу:

   ```bash
   python3 "$HOME/.claude/skills/freeze/bin/freeze.py" set <каталог>
   ```

3. Подтверди человеку: «правки разрешены только в <путь>».

С этого момента любая правка файла вне границы **запрещается** хуком, а не просто предупреждает. Чтение, поиск и команды Bash не ограничиваются: `/freeze` защищает от случайной правки, а не от намеренных действий.

- Показать границу: `python3 "$HOME/.claude/skills/freeze/bin/freeze.py" show`
- Снять: `/unfreeze`

Граница хранится в `~/.claude/vw-state/` отдельно для каждого проекта. Хук работает только в сессии, где вызван `/freeze`; в новой сессии граница не действует, пока `/freeze` не вызовут снова.

Источник идеи — gstack `/freeze` (MIT), см. `NOTICE.md` в корне workflow.
