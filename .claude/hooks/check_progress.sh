#!/usr/bin/env bash
# SessionStart hook: detect in-progress tasks in CLAUDE.md §8 and point agent
# to their plan files. Silent when nothing is in progress.
set -u

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

CLAUDE_MD="CLAUDE.md"
[ -f "$CLAUDE_MD" ] || exit 0

# Extract content between the "### 進行中" subheading and the next ### or ## heading.
section=$(awk '
    /^### 進行中/ { in_section=1; next }
    in_section && (/^###/ || /^## /) { exit }
    in_section { print }
' "$CLAUDE_MD")

# Lines starting with "- [Fx]" or "- [Bx]" are tasks; "_目前無_" is the empty placeholder.
task_lines=$(printf '%s\n' "$section" | grep -E '^[[:space:]]*- \[[BF][0-9]+\]' || true)

[ -z "$task_lines" ] && exit 0

echo ""
echo "進行中任務（CLAUDE.md §8）— 本 session 請接續推進："

while IFS= read -r line; do
    id=$(printf '%s' "$line" | grep -oE '\[[BF][0-9]+\]' | head -1 | tr -d '[]')
    [ -z "$id" ] && continue
    plan=$(ls "docs/plans/${id}-"*.md 2>/dev/null | head -1)
    if [ -n "$plan" ]; then
        echo "  - [$id] -> 請讀 @${plan}"
    else
        echo "  - [$id] -> 對應 plan 檔尚未建立（預期 docs/plans/${id}-*.md）"
    fi
done <<< "$task_lines"
