<#
.SYNOPSIS
    每日同步脚本：把本地项目（脱敏后）提交并推送到 GitHub。

.DESCRIPTION
    1) 运行 sanitize_workflow.py 生成脱敏版工作流；
    2) git add -A；
    3) 仅当有变更时才 commit + push（"如果更新了相关内容才同步"）；
    4) 全过程写入 logs/sync-*.log。

    由 Windows 计划任务每日触发（见 install_task.ps1），也可手动运行。
#>
param(
    [string]$RepoDir = "G:\workbuddy\workspace\n8nfeishucundangv2",
    [string]$Branch  = "main",
    [string]$Remote  = "origin"
)

# 计划任务环境的 PATH 可能与交互式不同：显式补上 git / python 所在目录（存在才加）
$gitBin = "C:\Users\admin\.workbuddy\binaries\PortableGit\versions\1.3.0\mingw64\bin"
$pyBin  = "C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12"
foreach ($p in @($gitBin, $pyBin)) {
    if ((Test-Path $p) -and ($env:Path -notlike "*$p*")) { $env:Path = "$p;$env:Path" }
}

$ErrorActionPreference = "Continue"

if (-not (Test-Path $RepoDir)) {
    Write-Error "仓库目录不存在：$RepoDir"
    exit 2
}
Set-Location $RepoDir

$logDir = Join-Path $RepoDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$log   = Join-Path $logDir "sync-$stamp.log"

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

Log "==== 每日同步开始（branch=$Branch remote=$Remote）===="

# 1) 脱敏：运行版 -> 可公开版
try {
    $out = & python "scripts\sanitize_workflow.py" 2>&1
    Log ("脱敏：" + ($out -join " | "))
} catch {
    Log "脱敏失败（继续提交现有内容）：$($_.Exception.Message)"
}

# 2) 暂存
git add -A 2>&1 | ForEach-Object { Log "git add | $_" }

# 3) 判断是否有变更
$porcelain = git status --porcelain
if ([string]::IsNullOrWhiteSpace($porcelain)) {
    Log "无变更，跳过提交/推送。"
    Log "==== 结束（无变更）===="
    exit 0
}
Log ("检测到变更：" + (($porcelain -split "`n").Count) + " 项")

# 4) 提交
$msg = "chore: auto daily sync {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
git commit -m $msg 2>&1 | ForEach-Object { Log "git commit | $_" }
if ($LASTEXITCODE -ne 0) {
    Log "提交失败（退出码 $LASTEXITCODE）。"
    Log "==== 结束（失败）===="
    exit 1
}

# 5) 推送
$push = git push $Remote $Branch 2>&1
Log ("git push | " + ($push -join " | "))
if ($LASTEXITCODE -ne 0) {
    Log "推送失败（退出码 $LASTEXITCODE）。请检查网络 / git 凭据（PAT）。"
    Log "==== 结束（失败）===="
    exit 1
}

Log "推送成功。"
Log "==== 结束（成功）===="
exit 0
