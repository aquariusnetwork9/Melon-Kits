<#
.SYNOPSIS
    Check a Windows install of the Melon Kits bot before starting it for real.

.DESCRIPTION
    Every check here is one I could not run for you, so each one fails loudly rather than
    warning quietly. Re-runnable at any time; it starts nothing that keeps running.

    The load-bearing check is the last one: it starts the bot with a deliberately invalid
    token and expects exit code 3. Reaching "Discord rejected the token" proves the imports,
    the config, the ledger path, the state directory being writable AND that this box can
    reach Discord's API at all -- without needing the real token and without a second bot
    ever connecting to your guild.

    Windows PowerShell 5.1 compatible.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File verify.ps1
    powershell -ExecutionPolicy Bypass -File verify.ps1 -StateDir D:\melonkit
#>
[CmdletBinding()]
param(
    [string]$Checkout = "",
    [string]$StateDir = "C:\ProgramData\melonkit"
)

$ErrorActionPreference = "Continue"
$script:Failed = 0
$script:Warned = 0

function Ok   { param($m) Write-Host "  PASS $m" -ForegroundColor Green }
function Bad  { param($m) Write-Host "  FAIL $m" -ForegroundColor Red;    $script:Failed++ }
function Warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow; $script:Warned++ }

if ($Checkout -eq "") {
    $Checkout = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$KitApp     = Join-Path $Checkout "kit-app"
$VenvPy     = Join-Path $KitApp ".venv\Scripts\python.exe"
$ConfigPath = Join-Path $StateDir "melonkit.json"
$LedgerPath = Join-Path $StateDir "melonkit.sqlite3"

Write-Host ""
Write-Host "Melon Kits - Windows verification" -ForegroundColor Cyan
Write-Host "  checkout: $Checkout"
Write-Host "  state   : $StateDir"
Write-Host ""

# --- 1. interpreter ---------------------------------------------------------------------
if (Test-Path $VenvPy) {
    $v = & $VenvPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1
    Ok "venv interpreter present (Python $v)"
    $dp = & $VenvPy -c "import discord; print(discord.__version__)" 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "discord.py $dp" } else { Bad "discord.py does not import: $dp" }
} else {
    Bad "no venv at $VenvPy - run install.ps1"
}

# --- 2. config --------------------------------------------------------------------------
$cfg = $null
if (-not (Test-Path $ConfigPath)) {
    Bad "no config at $ConfigPath"
} else {
    try {
        $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        Ok "config parses as JSON"
    } catch {
        # By far the most likely cause on Windows, and the raw error does not say so.
        Bad "config is not valid JSON: $($_.Exception.Message)"
        Write-Host "       If the paths use backslashes, that is why: C:\Users\... is invalid" -ForegroundColor Yellow
        Write-Host "       JSON because \U is an escape. Use forward slashes." -ForegroundColor Yellow
    }
}

if ($cfg) {
    $ids = @{
        "home_guild_id"         = $cfg.discord.home_guild_id
        "panel_channel_id"      = $cfg.discord.panel_channel_id
        "queue_channel_id"      = $cfg.discord.queue_channel_id
        "transcript_channel_id" = $cfg.discord.transcript_channel_id
    }
    $zero = @()
    foreach ($k in $ids.Keys) { if (-not $ids[$k] -or $ids[$k] -eq 0) { $zero += $k } }
    if ($zero.Count -gt 0) {
        Bad ("still at 0: " + ($zero -join ", ") + " - copy melonkit.json from the export bundle")
    } else {
        Ok "channel and guild ids are set"
    }

    $store = $cfg.store.path
    if (-not $store) {
        Bad "store.path is missing"
    } elseif ($store -notmatch '^[A-Za-z]:[/\\]') {
        # A relative path resolves against the working directory. Under a console launched
        # from anywhere but the right folder -- or later under a scheduled task, whose
        # "Start in" box is easy to leave blank -- the ledger silently lands elsewhere and
        # the bot comes up with an empty history.
        Bad "store.path '$store' is not absolute - the ledger will follow the working directory"
    } else {
        Ok "store.path is absolute ($store)"
    }
    if ($cfg.ops -and $cfg.ops.log_path) {
        Ok "ops.log_path set ($($cfg.ops.log_path)) - there is no journald here, so this is the log"
    } else {
        Warn "ops.log_path is not set; output only goes to the console and is lost when it closes"
    }
}

# --- 3. ledger --------------------------------------------------------------------------
if (-not (Test-Path $LedgerPath)) {
    Warn "no ledger at $LedgerPath yet - fine for a fresh install, WRONG if you are migrating"
} else {
    $probe = @"
import sqlite3, sys
db = sqlite3.connect('file:' + sys.argv[1].replace('\\', '/') + '?mode=ro', uri=True)
v = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
t = db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
k = db.execute('SELECT COUNT(*) FROM kits').fetchone()[0]
c = db.execute('SELECT COUNT(*) FROM kit_claims').fetchone()[0]
print('%s|%s|%s|%s' % (v, t, k, c))
"@
    $tmp = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -Path $tmp -Value $probe -Encoding UTF8
    $out = & $VenvPy $tmp $LedgerPath 2>&1
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -eq 0 -and $out -match '\|') {
        $f = $out.ToString().Trim().Split('|')
        Ok "ledger opens: schema $($f[0]), $($f[1]) tickets, $($f[2]) kits, $($f[3]) claims"
        if ([int]$f[0] -lt 6) { Warn "schema $($f[0]) will be upgraded to 6 on first start" }
    } else {
        Bad "ledger will not open: $out"
    }

    # A -wal beside the ledger after a migration means the copy came from a live database
    # and may be missing recent writes. The export script checkpoints precisely to avoid it.
    if (Test-Path ($LedgerPath + "-wal")) {
        $walLen = (Get-Item ($LedgerPath + "-wal")).Length
        if ($walLen -gt 0) { Warn "a non-empty -wal sits beside the ledger; if this came from the Linux box, re-export with the checkpoint step" }
    }
}

# --- 4. token ---------------------------------------------------------------------------
$tok = [Environment]::GetEnvironmentVariable("MELONKIT_DISCORD_TOKEN", "Machine")
if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("MELONKIT_DISCORD_TOKEN", "User") }
if (-not $tok) { $tok = $env:MELONKIT_DISCORD_TOKEN }
if (-not $tok) {
    Bad "MELONKIT_DISCORD_TOKEN is not set - setx MELONKIT_DISCORD_TOKEN ""<token>"" /M (as Administrator)"
} elseif ($tok.Length -lt 50) {
    Bad "MELONKIT_DISCORD_TOKEN is set but looks too short to be a bot token"
} else {
    Ok "MELONKIT_DISCORD_TOKEN is set"
    Warn "a machine environment variable is readable by every process on this box; see HARDENING"
}

# --- 5. the real check ------------------------------------------------------------------
if ((Test-Path $VenvPy) -and $cfg) {
    Write-Host ""
    Write-Host "  starting the bot with a deliberately invalid token..." -ForegroundColor Cyan
    $bot = Join-Path $KitApp "bot.py"
    $prev = $env:MELONKIT_DISCORD_TOKEN
    $env:MELONKIT_DISCORD_TOKEN = "verify.invalid.token"
    try {
        $output = & $VenvPy $bot --config $ConfigPath 2>&1
        $code = $LASTEXITCODE
    } finally {
        $env:MELONKIT_DISCORD_TOKEN = $prev
    }
    if ($code -eq 3 -or ($output -join "`n") -match "rejected the token") {
        Ok "reached Discord and was rejected - imports, config, ledger and network all work"
    } else {
        Bad "expected exit 3 (token rejected); got exit $code"
        ($output | Select-Object -Last 12) | ForEach-Object { Write-Host "       $_" }
    }
}

Write-Host ""
if ($script:Failed -gt 0) {
    Write-Host "$($script:Failed) failed, $($script:Warned) warning(s). Fix the failures before starting." -ForegroundColor Red
    exit 1
}
Write-Host "All checks passed ($($script:Warned) warning(s))." -ForegroundColor Green
Write-Host "Before you start: make sure the LINUX service is stopped and disabled." -ForegroundColor Yellow
Write-Host "One token, one bot - two connected at once both act on every press." -ForegroundColor Yellow
exit 0
