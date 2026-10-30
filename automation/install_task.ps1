<#
.SYNOPSIS
    注册 Windows 计划任务：每日定时运行 daily_sync.ps1。

.NOTES
    请以【管理员身份】打开 PowerShell 执行一次：
        powershell -ExecutionPolicy Bypass -File automation\install_task.ps1
    默认每日 23:30 运行，任务名 n8n-feishu-daily-sync。
#>
param(
    [string]$TaskName = "n8n-feishu-daily-sync",
    [string]$Time     = "23:30"
)

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "daily_sync.ps1"

if (-not (Test-Path $script)) {
    Write-Error "找不到 daily_sync.ps1：$script"
    exit 2
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger  = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "每日自动同步 n8n 飞书存档机器人项目到 GitHub（脱敏后）" `
    -Force | Out-Null

Write-Host "✔ 已注册计划任务：$TaskName（每日 $Time 运行）"
Write-Host "  查看：Get-ScheduledTask -TaskName $TaskName"
Write-Host "  立即运行一次：Start-ScheduledTask -TaskName $TaskName"
Write-Host "  删除：Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false"
