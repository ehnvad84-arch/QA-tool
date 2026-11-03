#!/usr/bin/env bash
# Ralph loop — автономный режим Этапа 2 (.workflow/WORKFLOW.md).
# Каждая итерация — новый процесс агента с чистым контекстом и одна задача из plan.md.
# Состояние живёт только в plan.md, progress.md и Git.
#
#   .workflow/ralph.sh <slug> [max_iterations=10]
#
# RALPH_ALLOW — команды проекта, которые агенту можно запускать, через «;»,
#               например 'Bash(npm test:*);Bash(npm run lint:*)'
# RALPH_AGENT — другой агент, читающий промпт из stdin, например 'codex exec -'
set -euo pipefail

slug="${1:?usage: ralph.sh <slug> [max_iterations]}"
max="${2:-10}"
root="$(git rev-parse --show-toplevel)"
plan="$root/docs/work/$slug/plan.md"
log="$root/.workflow/ralph-$slug.log"

[ -f "$plan" ] || { echo "Нет плана: $plan" >&2; exit 1; }
case "$(git -C "$root" branch --show-current)" in
  main|master|"") echo "Ralph loop работает только в feature-ветке или worktree." >&2; exit 1 ;;
esac

if [ -n "${RALPH_AGENT:-}" ]; then
  read -ra agent <<< "$RALPH_AGENT"
else
  [ -n "${RALPH_ALLOW:-}" ] || {
    echo "Задай RALPH_ALLOW — команды тестов и линтера, иначе агент не сможет проверить итерацию." >&2
    exit 1
  }
  # Минимальные права: правка файлов, git для фиксации и только перечисленные команды проекта.
  agent=(claude -p --model sonnet --permission-mode acceptEdits --allowedTools
         "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)"
         "Bash(git add:*)" "Bash(git commit:*)" "Bash(git restore:*)")
  IFS=';' read -ra extra <<< "$RALPH_ALLOW"
  agent+=("${extra[@]}")
fi

prompt="$(sed "s/<slug>/$slug/g" "$root/.workflow/prompts/ralph-iteration.md")"

for ((i = 1; i <= max; i++)); do
  if grep -qE '^- \[!\]' "$plan"; then
    echo "В плане блокер [!] — причина в progress.md. Стоп."
    exit 2
  fi
  next="$(grep -m1 -E '^- \[ \] T[0-9]+' "$plan" || true)"
  if [ -z "$next" ]; then
    echo "Все задачи [x]. Дальше — ручная проверка пачки и Этап 3 (ревью)."
    exit 0
  fi
  case "$next" in
    *"проверка: manual"*) echo "Следующая задача требует ручной проверки — стоп: $next"; exit 0 ;;
  esac
  if [ -n "$(git -C "$root" status --porcelain)" ]; then
    echo "Рабочее дерево не чистое — нет точки отката. Стоп." >&2
    exit 2
  fi

  head_before="$(git -C "$root" rev-parse HEAD)"
  echo "── итерация $i/$max: $next" | tee -a "$log"
  if ! printf '%s\n' "$prompt" | "${agent[@]}" 2>&1 | tee -a "$log"; then
    echo "Агент завершился с ошибкой — стоп, смотри $log" >&2
    exit 2
  fi
  if [ "$(git -C "$root" rev-parse HEAD)" = "$head_before" ]; then
    echo "Итерация не сделала коммит — стоп, смотри $log" >&2
    exit 2
  fi
done

echo "Лимит итераций ($max) исчерпан."
