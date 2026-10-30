param(
    [string]$RepoDir = "G:\workbuddy\workspace\n8nfeishucundangv2",
    [string]$Branch  = "main",
    [string]$Remote  = "origin"
)

$ErrorActionPreference = "Continue"

# ---- resolve absolute tool paths (robust under Task Scheduler) ----
$gitExe = "C:\Users\admin\.workbuddy\binaries\PortableGit\versions\1.3.0\mingw64\bin\git.exe"
if (-not (Test-Path $gitExe)) { $gitExe = "git" }
$pyExe  = "C:\Users\admin\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if (-not (Test-Path $pyExe))  { $pyExe  = "python" }

$logDir = Join-Path $RepoDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$script:log = Join-Path $logDir ("sync-" + (Get-Date -Format "yyyy-MM-dd_HHmmss") + ".log")

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    try { Add-Content -LiteralPath $script:log -Value $line -Encoding utf8 } catch {}
    Write-Host $line
}

Log "==== daily sync start (branch=$Branch remote=$Remote repo=$RepoDir) ===="

# 1) sanitize (read secrets from local .env -> write public workflow)
try {
    $out = & $pyExe (Join-Path $RepoDir "scripts\sanitize_workflow.py") 2>&1
    Log ("sanitize: " + (($out | Out-String).Trim()))
} catch {
    Log ("sanitize failed: " + $_.Exception.Message)
}

# 2) stage all
try {
    $addOut = & $gitExe -C $RepoDir add -A 2>&1
    if ($addOut) { Log ("git add: " + (($addOut | Out-String).Trim())) }
} catch {
    Log ("git add failed: " + $_.Exception.Message)
}

# 3) detect changes
$porcelain = & $gitExe -C $RepoDir status --porcelain 2>&1
$pText = ($porcelain | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($pText)) {
    Log "no changes; skip commit/push"
    Log "==== end (no change) ===="
    exit 0
}
$count = ($porcelain | Measure-Object).Count
Log ("changes detected: $count file(s)")

# 4) commit
$msg = "chore: auto daily sync " + (Get-Date -Format "yyyy-MM-dd HH:mm")
$c = & $gitExe -C $RepoDir commit -m $msg 2>&1
$crc = $LASTEXITCODE
Log ("git commit: " + (($c | Out-String).Trim()))
if ($crc -ne 0) {
    Log ("git commit failed: exit $crc")
    Log "==== end (fail) ===="
    exit 1
}

# 5) push (GCM credentials come from the logged-on user's Windows Credential Manager)
try {
    $p = & $gitExe -C $RepoDir push $Remote $Branch 2>&1
    $rc = $LASTEXITCODE
    Log ("git push: " + (($p | Out-String).Trim()))
    if ($rc -ne 0) { throw "push exit code $rc" }
} catch {
    Log ("git push failed: " + $_.Exception.Message)
    Log "==== end (fail) ===="
    exit 1
}

Log "push ok"
Log "==== end (ok) ===="
exit 0
