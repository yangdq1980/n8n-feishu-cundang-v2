<#
.SYNOPSIS
    Register a Windows Scheduled Task that runs daily_sync.sh (via Git-Bash) every day.

.NOTES
    Run once:
        powershell -ExecutionPolicy Bypass -File automation\install_task.ps1
    Default: daily at 23:30, task name n8n-feishu-daily-sync.
    This file is ASCII-only to avoid Windows PowerShell 5.1 encoding issues.
#>
param(
    [string]$TaskName = "n8n-feishu-daily-sync",
    [string]$Time     = "23:30"
)

$bashExe = "C:\Users\admin\.workbuddy\binaries\PortableGit\versions\1.3.0\bin\bash.exe"
if (-not (Test-Path $bashExe)) { Write-Error "bash.exe not found: $bashExe"; exit 2 }

$shPath = "/g/workbuddy/workspace/n8nfeishucundangv2/automation/daily_sync.sh"
$repo   = "G:\workbuddy\workspace\n8nfeishucundangv2"

$action   = New-ScheduledTaskAction -Execute $bashExe -Argument "-lc `"bash $shPath`"" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily sync n8n feishu archive project to GitHub (bash)" -Force | Out-Null

Write-Host "OK: task '$TaskName' registered (daily $Time)"
Write-Host "  Task info : Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Run now   : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove    : Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
