#!/usr/bin/env bash
# 每日同步（Bash 版）：脱敏 -> 提交 -> 推送。由 Windows 计划任务调用 Git-Bash 执行。
# 相比 PowerShell 版，规避了 PS 5.1 的 index.lock / 编码 / 作用域问题。
set -uo pipefail

REPO="/g/workbuddy/workspace/n8nfeishucundangv2"
BRANCH="main"
REMOTE="origin"

cd "$REPO" || { echo "repo not found: $REPO"; exit 2; }

LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y-%m-%d_%H%M%S')"
LOG="$LOG_DIR/sync-$STAMP.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "==== daily sync start (branch=$BRANCH repo=$REPO) ===="

# 清理可能残留的锁（例如上次异常中断）
rm -f "$REPO/.git/index.lock" 2>/dev/null

# 1) 脱敏：运行版 -> 可公开版
if python "$REPO/scripts/sanitize_workflow.py" >>"$LOG" 2>&1; then
  log "sanitize ok"
else
  log "sanitize failed (continue anyway)"
fi

# 2) 暂存
git add -A >>"$LOG" 2>&1

# 3) 判断是否有变更
if [ -z "$(git status --porcelain)" ]; then
  log "no changes; skip commit/push"
  log "==== end (no change) ===="
  exit 0
fi
CHANGES="$(git status --porcelain | wc -l | tr -d ' ')"
log "changes detected: $CHANGES file(s)"

# 4) 提交
if git -c core.autocrlf=false commit -m "chore: auto daily sync $(date '+%Y-%m-%d %H:%M')" >>"$LOG" 2>&1; then
  log "commit ok"
else
  log "commit failed"
  log "==== end (fail) ===="
  exit 1
fi

# 5) 推送（GCM 凭据来自当前登录用户的 Windows 凭据管理器）
if GIT_TERMINAL_PROMPT=0 git push "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
  log "push ok"
  log "==== end (ok) ===="
else
  log "push failed"
  log "==== end (fail) ===="
  exit 1
fi
