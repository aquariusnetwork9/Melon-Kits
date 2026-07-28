<#
.SYNOPSIS
    Set up the Melon Kits bot on a Windows host.

.DESCRIPTION
    Creates the virtual environment, installs the one dependency, lays out the state directory
    and writes a config with ABSOLUTE paths. It does not start anything and does not touch the
    token.

    Written for Windows PowerShell 5.1, which is what Windows 10 ships. No PS7-only syntax
    (no ternary, no ??, no -Parallel) -- a script that only runs on the version you happen to
    have installed is a script that fails on the box it was written for.

.PARAMETER Checkout
    Where the repo is cloned. Defaults to the parent of this script's grandparent.

.PARAMETER StateDir
    Where the ledger, config and lexicon live. Kept OUTSIDE the checkout on purpose: the
    ledger is the one thing here that cannot be regenerated, and a pull or a re-clone must
    never be able to reach it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -StateDir D:\melonkit
#>
[CmdletBinding()]
param(
    [string]$Checkout = "",
    [string]$StateDir = "C:\ProgramData\melonkit"
)

$ErrorActionPreference = "Stop"

function Say  { param($m) Write-Host "  $m" }
function Ok   { param($m) Write-Host "  OK   $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

if ($Checkout -eq "") {
    # deploy\windows\install.ps1 -> kit-app -> repo root
    $Checkout = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$KitApp = Join-Path $Checkout "kit-app"
if (-not (Test-Path (Join-Path $KitApp "bot.py"))) {
    Die "no bot.py under $KitApp -- pass -Checkout with the repo path"
}

Write-Host ""
Write-Host "Melon Kits - Windows install" -ForegroundColor Cyan
Say "checkout : $Checkout"
Say "state    : $StateDir"
Write-Host ""

# --- Python -----------------------------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Die "Python is not on PATH. Install 3.9 or newer from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}
$verText = & $py.Source -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
$parts = $verText.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Die "Python $verText is too old; 3.9 or newer is required."
}
Ok "Python $verText at $($py.Source)"

# --- venv -------------------------------------------------------------------------------
$VenvDir = Join-Path $KitApp ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $VenvPy) {
    Ok "venv already present"
} else {
    Say "creating venv..."
    & $py.Source -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) { Die "venv creation produced no python.exe at $VenvPy" }
    Ok "venv created"
}

Say "installing discord.py..."
& $VenvPy -m pip install --quiet --disable-pip-version-check --upgrade pip | Out-Null
& $VenvPy -m pip install --quiet --disable-pip-version-check "discord.py>=2.4,<3"
$dpv = & $VenvPy -c "import discord; print(discord.__version__)"
if ($LASTEXITCODE -ne 0) { Die "discord.py did not import after install" }
Ok "discord.py $dpv"

# --- state directory --------------------------------------------------------------------
if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    Ok "created $StateDir"
} else {
    Ok "state directory exists"
}

# The ledger holds redacted chat plus every decision and its reason. Strip inherited access
# so it is not readable by every user on the box; this is the rough equivalent of the
# UMask=0027 the systemd unit ran under.
try {
    $acl = Get-Acl $StateDir
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($who in @("$env:USERDOMAIN\$env:USERNAME", "BUILTIN\Administrators", "NT AUTHORITY\SYSTEM")) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
        $acl.AddAccessRule($rule)
    }
    Set-Acl -Path $StateDir -AclObject $acl
    Ok "locked $StateDir to you, Administrators and SYSTEM"
} catch {
    Warn "could not tighten permissions on ${StateDir}: $($_.Exception.Message)"
    Warn "the ledger contains chat and decisions - restrict it by hand"
}

# --- config -----------------------------------------------------------------------------
# Forward slashes throughout. A Windows path pasted into JSON with backslashes is INVALID
# JSON -- "C:\Users\..." fails to parse because \U is an escape sequence - and the resulting
# error names a column number, not the real problem.
$ConfigPath = Join-Path $StateDir "melonkit.json"
$LedgerPath = ($StateDir + "\melonkit.sqlite3") -replace '\\', '/'
$LogPath    = ($StateDir + "\melonkit.log")     -replace '\\', '/'
$LexPath    = ($StateDir + "\lexicon.json")     -replace '\\', '/'

if (Test-Path $ConfigPath) {
    Ok "config already at $ConfigPath (left alone)"
    Say "check its store.path is absolute - see MIGRATION in DEPLOY-WINDOWS.md"
} else {
    $tmpl = @"
{
  "discord": {
    "home_guild_id": 0,
    "panel_channel_id": 0,
    "queue_channel_id": 0,
    "transcript_channel_id": 0,
    "reviewer_role_id": 0,
    "runner_role_id": 0
  },
  "store": { "path": "$LedgerPath" },
  "screening": { "lexicon_path": "$LexPath" },
  "ops": { "log_path": "$LogPath", "log_level": "INFO" }
}
"@
    # NOT Set-Content -Encoding UTF8: on PowerShell 5.1 -- which is what Windows 10 ships --
    # that writes a byte-order mark, where PowerShell 7 does not. Three invisible bytes at the
    # start of the file, and the loader reports a JSON error at "line 1 column 1".
    [System.IO.File]::WriteAllText($ConfigPath, $tmpl, (New-Object System.Text.UTF8Encoding($false)))
    Ok "wrote a starter config to $ConfigPath"
    Warn "the ids are all 0 - replace it with the melonkit.json from the export bundle"
}

Write-Host ""
Write-Host "Installed. Next:" -ForegroundColor Cyan
Write-Host "  1. Copy melonkit.json, melonkit.sqlite3 and lexicon.json from the export"
Write-Host "     bundle into $StateDir (overwriting the starter config), then fix"
Write-Host "     store.path / ops.log_path / screening.lexicon_path to the Windows paths."
Write-Host "  2. setx MELONKIT_DISCORD_TOKEN ""<token>"" /M     (run as Administrator)"
Write-Host "  3. powershell -ExecutionPolicy Bypass -File verify.ps1"
Write-Host "  4. run.cmd"
Write-Host ""
