#!/usr/bin/env bash
# Stop hook: warn when working tree changed but SESSION_LOG.md was not updated.
#
# Detection covers all change kinds:
#   - tracked + modified (unstaged)    →  M_  /  _M  in porcelain
#   - tracked + staged                 →  M_ / A_ / D_ / R_ ...
#   - untracked (NEW files)            →  ??           ← this is what `git diff` misses
#
# Decision matrix (using `git status --porcelain`):
#   total == 0                              → silent (nothing happened)
#   SESSION_LOG.md among the changed paths  → ✅ updated
#   anything else changed, log unchanged    → ⚠ warn
set -u

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Trim leading whitespace from `wc -l` output (BSD wc on macOS pads with spaces).
total=$(git status --porcelain 2>/dev/null | wc -l | tr -d '[:space:]')
log=$(git status --porcelain -- SESSION_LOG.md 2>/dev/null | wc -l | tr -d '[:space:]')

if [ "$total" -eq 0 ]; then
    exit 0
fi

if [ "$log" -gt 0 ]; then
    echo "✅ SESSION_LOG.md 已更新"
else
    echo "⚠️  警告：本 session 有程式碼變動但 SESSION_LOG.md 未更新。請依 CLAUDE.md §2.1 格式補上紀錄後再結束。"
fi
