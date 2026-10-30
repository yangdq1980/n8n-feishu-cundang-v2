@echo off
REM ============================================================
REM 手动一键同步：脱敏 -> 提交 -> 推送到 GitHub
REM 双击即可运行（等价于计划任务的一次触发）
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0daily_sync.ps1"
echo.
echo [完成] 详细日志见 logs\ 目录
pause
