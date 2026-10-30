@echo off
REM ============================================================
REM 手动一键同步：脱敏 -> 提交 -> 推送到 GitHub（通过 Git-Bash 执行 daily_sync.sh）
REM 双击即可运行（等价于计划任务的一次触发）
REM ============================================================
"C:\Users\admin\.workbuddy\binaries\PortableGit\versions\1.3.0\bin\bash.exe" -lc "bash /g/workbuddy/workspace/n8nfeishucundangv2/automation/daily_sync.sh"
echo.
echo [done] see logs\ for details
pause
