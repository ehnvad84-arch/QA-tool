#!/usr/bin/env bash
# Новая фича: ветка feature/<slug> и папка docs/work/<slug>/ со спекой, планом и журналом из шаблонов.
#   .workflow/feature.sh <slug>
set -euo pipefail

slug="${1:?usage: feature.sh <slug>}"
case "$slug" in
  *[!a-z0-9-]*) echo "slug — только a-z, 0-9 и дефис: $slug" >&2; exit 1 ;;
esac

root="$(git rev-parse --show-toplevel)"
dir="$root/docs/work/$slug"
[ -e "$dir" ] && { echo "Уже существует: docs/work/$slug" >&2; exit 1; }
[ -n "$(git -C "$root" status --porcelain)" ] && { echo "Рабочее дерево не чистое — сначала коммит." >&2; exit 1; }

base="$(git -C "$root" branch --show-current)"
git -C "$root" switch -c "feature/$slug"
mkdir -p "$dir"
for name in feature-spec plan progress; do
  sed "s/<slug>/$slug/g" "$root/.workflow/templates/work/$name.md" > "$dir/$name.md"
done

echo "Ветка feature/$slug (от $base), артефакты в docs/work/$slug/."
echo "Дальше — Этап 1 (.workflow/WORKFLOW.md) или промпт .workflow/prompts/plan.md."
