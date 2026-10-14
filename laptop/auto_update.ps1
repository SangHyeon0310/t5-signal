# One-time setup + daily auto update of ST-TRA and T5 on the laptop. ASCII only (Windows PowerShell 5.1).
# After the first run you never download ZIP files again: git pulls the latest code every day.
#  1. installs Git for Windows if missing (to D:\ST\tools\Git when D: exists)
#  2. turns the existing ST-TRA / T5 folders into git checkouts IN PLACE
#     (same paths, so scheduled tasks, venvs, work folders, local switches and API key are untouched)
#  3. registers task "ST-AutoUpdate" (daily 06:30 + at logon) that pulls both repos
#  4. on the first (interactive) run, shows the C: drive report
# GitHub login: the first time, a browser window asks you to sign in to GitHub once (private repos).
param([switch]$Quiet)
$ErrorActionPreference = "Stop"
$Branch = "claude/funny-ramanujan-2a6a0i"
$Base = if (Test-Path "D:\") { "D:\ST" } else { Join-Path $env:USERPROFILE "ST" }
$LogDir = Join-Path $Base "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "auto_update.log"

function Say([string]$m, [string]$c = "Gray") {
    $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $m
    Add-Content -Path $Log -Value $line
    if (-not $Quiet) { Write-Host $m -ForegroundColor $c }
}

function Find-Git {
    $g = Get-Command git -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    foreach ($p in @("D:\ST\tools\Git\cmd\git.exe", "C:\Program Files\Git\cmd\git.exe",
                     (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"))) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

# ---- 1. Git ----
$Git = Find-Git
if (-not $Git) {
    if ($Quiet) { Say "git missing; run the setup command once by hand" "Red"; exit 1 }
    Say "Installing Git for Windows (one time)..." "Cyan"
    $wg = @("install", "--id", "Git.Git", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements")
    try {
        if (Test-Path "D:\") { & winget @wg --location "D:\ST\tools\Git" } else { & winget @wg }
        $Git = Find-Git
        if (-not $Git -and (Test-Path "D:\")) { & winget @wg; $Git = Find-Git }
    } catch { Say ("winget: " + $_.Exception.Message) "Yellow" }
    if (-not $Git) { Say "Git install failed. Install from https://git-scm.com/download/win and run again." "Red"; exit 1 }
}
Say ("git: " + $Git)
# D: may be exFAT/FAT32 (no owner info): allow these folders so git does not refuse them.
$safe = @(& $Git config --global --get-all safe.directory)
if ($safe -notcontains "*") { & $Git config --global --add safe.directory "*" }

# ---- 2. Repos ----
function Find-Folder([string]$marker, [string]$venv, [string[]]$roots) {
    # Prefer the copy that is already set up (has its venv), then the most recently changed one.
    $hits = foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem -Path $r -Filter $marker -File -Recurse -Depth 3 -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch "\\\.venv" }
        }
    }
    $best = $hits | Sort-Object @{ Expression = { Test-Path (Join-Path $_.DirectoryName $venv) }; Descending = $true },
                                @{ Expression = { $_.LastWriteTime }; Descending = $true } | Select-Object -First 1
    if ($best) { return $best.DirectoryName }
    return $null
}

$Roots = @("D:\ST", "C:\ST", (Join-Path $env:USERPROFILE "ST"), (Join-Path $env:USERPROFILE "ST-TRA-FASTTRACK"))
$Repos = @(
    @{ Name = "ST-TRA"; Url = "https://github.com/SangHyeon0310/ST-TRA.git"; Marker = "SETUP-TRA-PAPER.cmd";
       Default = (Join-Path $Base "TRA-FASTTRACK\ST-TRA"); Venv = ".venv-paper"; Req = "requirements-fasttrack.txt" },
    @{ Name = "T5"; Url = "https://github.com/SangHyeon0310/T5.git"; Marker = "SETUP-T5.cmd";
       Default = (Join-Path $Base "T5\T5"); Venv = ".venv-t5"; Req = "requirements.txt" }
)

$failed = 0
$TraDir = $null
foreach ($r in $Repos) {
    $dir = Find-Folder $r.Marker $r.Venv $Roots
    if (-not $dir) { $dir = $r.Default; Say ("  " + $r.Name + " not found; fresh copy (run its SETUP once)") "Yellow" }
    if ($r.Name -eq "ST-TRA") { $TraDir = $dir }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Say ("[" + $r.Name + "] " + $dir) "Cyan"
    $reqFile = Join-Path $dir $r.Req
    $reqBefore = if (Test-Path $reqFile) { (Get-FileHash $reqFile).Hash } else { "" }
    if (-not (Test-Path (Join-Path $dir ".git"))) {
        # In-place conversion: tracked files are replaced by the branch version,
        # untracked files (.venv, work-folder pointers, *.local.json) stay as they are.
        & $Git -C $dir init -q
        & $Git -C $dir remote add origin $r.Url
        & $Git -C $dir fetch -q origin ("+refs/heads/" + $Branch + ":refs/remotes/origin/" + $Branch)
        if ($LASTEXITCODE -ne 0) { Say ("  fetch failed (GitHub login?)") "Red"; $failed++; continue }
        & $Git -C $dir checkout -q -f -B $Branch ("origin/" + $Branch)
        & $Git -C $dir branch -q -u ("origin/" + $Branch)
        Say "  converted to git checkout" "Green"
    } else {
        & $Git -C $dir fetch -q origin $Branch
        if ($LASTEXITCODE -ne 0) { Say "  fetch failed" "Red"; $failed++; continue }
        & $Git -C $dir merge -q --ff-only ("origin/" + $Branch)
        if ($LASTEXITCODE -ne 0) { Say "  local changes block the update; nothing overwritten. Send this to Claude." "Red"; $failed++; continue }
    }
    Say ("  now at " + (& $Git -C $dir log --oneline -1)) "Green"
    # Python packages: only when the requirements file changed and the venv already exists.
    $py = Join-Path $dir ($r.Venv + "\Scripts\python.exe")
    $reqAfter = if (Test-Path $reqFile) { (Get-FileHash $reqFile).Hash } else { "" }
    if ((Test-Path $py) -and ($reqAfter -ne $reqBefore) -and $reqAfter) {
        & $py -m pip install --disable-pip-version-check -q -r $reqFile
        Say "  python packages updated"
    }
}

# ---- 3. Daily task ----
$Self = Join-Path $Base "tools\auto_update.ps1"
New-Item -ItemType Directory -Force -Path (Split-Path $Self) | Out-Null
if ($PSCommandPath -and ($PSCommandPath -ne $Self)) { Copy-Item $PSCommandPath $Self -Force }
if (-not $Quiet) {
    $act = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"" + $Self + "`" -Quiet")
    $trg = @((New-ScheduledTaskTrigger -Daily -At "06:30"), (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME))
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "ST-AutoUpdate" -Action $act -Trigger $trg -Settings $set -Force | Out-Null
    Say "task ST-AutoUpdate registered (daily 06:30 and at logon)" "Green"
}

# ---- 4. C: report on the interactive run ----
if (-not $Quiet -and $TraDir) {
    $disk = Join-Path $TraDir "scripts\disk_c.ps1"
    if (Test-Path $disk) { Write-Host ""; & powershell -NoProfile -ExecutionPolicy Bypass -File $disk -Mode report }
}
if ($failed -gt 0) { exit 1 }
exit 0
