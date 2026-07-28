# Running the bot on Windows

The application is portable. Verified on Windows 10/11 with Python 3.9.13 and discord.py
2.7.1: the full test suite passes, the ledger comes up in WAL mode at the current schema, and
the bot reaches Discord and is rejected on a bad token exactly as it does on Linux. There is
no POSIX-specific code in `bot.py`, `store.py` or `config.py` — no hardcoded `/var/lib` or
`/etc`, no `umask`, signals, `fcntl` or `geteuid`.

**What does not port is the deployment layer.** systemd supplied start-at-boot, restart-on-
crash, a root-only token file, filesystem confinement and journald. Windows supplies none of
that, so this document replaces it — starting with the smallest thing that works.

---

## The rule that matters more than anything else here

**One token, one bot.** If the Linux service and the Windows one are ever connected at the
same time, Discord holds two gateway sessions and **both bots act on every interaction**: two
reviewer cards, two decisions recorded, two threads. Worse, each writes its own SQLite ledger,
so cooldowns and kit history diverge permanently and no merge is possible afterwards.

You cannot avoid this by pointing both at one file on a share. **SQLite in WAL mode does not
work over a network filesystem at all** — WAL needs shared memory a share cannot provide.

So the migration is: stop there, then start here. `export_from_linux.sh` stops *and disables*
the Linux service for you, so a reboot months later cannot quietly bring a second bot back.

---

## 1. On the Linux host

```bash
cd /home/ubuntu/melon-kits
bash kit-app/deploy/windows/export_from_linux.sh
```

It stops and disables `melonkit-bot`, checkpoints the WAL, bundles the ledger, `melonkit.json`
and `lexicon.json`, verifies the copy opens, and writes `melonkit-export-<timestamp>.tar.gz`.

**Why the checkpoint step exists:** `melonkit.sqlite3` on its own is the database as of the
last checkpoint — recent writes live in the `-wal` sidecar. Copying just the one file gives
you a database that opens cleanly and is silently missing the most recent tickets. There is no
error. Do not hand-copy the ledger without either checkpointing first or taking all three
files.

The token is **not** in the bundle. Read it out of `/etc/melonkit/env` (root-only) and carry it
across yourself.

Copy the tarball to the Windows box and unpack it.

## 2. On the Windows box

Install **Python 3.9 or newer** from python.org, ticking *Add python.exe to PATH*. Then:

```powershell
powershell -ExecutionPolicy Bypass -File kit-app\deploy\windows\install.ps1
```

This creates the venv, installs discord.py, creates `C:\ProgramData\melonkit`, restricts it to
you plus Administrators and SYSTEM, and writes a starter config. Pass `-StateDir D:\melonkit`
to put state elsewhere.

Now copy `melonkit.json`, `melonkit.sqlite3` and `lexicon.json` out of the bundle into
`C:\ProgramData\melonkit`, overwriting the starter config, and edit the three paths in the
config to their Windows equivalents:

```json
"store":     { "path": "C:/ProgramData/melonkit/melonkit.sqlite3" },
"screening": { "lexicon_path": "C:/ProgramData/melonkit/lexicon.json" },
"ops":       { "log_path": "C:/ProgramData/melonkit/melonkit.log", "log_level": "INFO" }
```

**Use forward slashes.** `"C:\ProgramData\..."` is *invalid JSON* — `\P` is not a valid escape
— and the parse error names a column number rather than the real problem. Doubled backslashes
work too; forward slashes read better and Windows accepts them everywhere here.

Set the token, as Administrator:

```cmd
setx MELONKIT_DISCORD_TOKEN "your-token-here" /M
```

`setx` writes the variable for *future* processes and does not affect the window it runs in —
open a new console afterwards, which is the usual reason the next step says the token is
missing twice in a row.

## 3. Check before you start

```powershell
powershell -ExecutionPolicy Bypass -File kit-app\deploy\windows\verify.ps1
```

It checks the interpreter, the config (including the backslash trap and whether `store.path`
is absolute), the ledger's schema and row counts, and the token. Its last check starts the bot
with a deliberately invalid token and expects exit 3 — reaching *"Discord rejected the token"*
proves imports, config, ledger and outbound network in one go, without the real token and
without a second bot ever touching your guild.

## 4. Run it

```cmd
kit-app\deploy\windows\run.cmd
```

It stays in the foreground. Ctrl-C stops it. The ledger is crash-safe, so an abrupt stop loses
nothing already committed.

## 5. Confirm it is really the only one

In Discord, press a button on a queue post. Exactly one response should happen. If anything
appears twice, the Linux service is still running — stop it immediately; every minute both are
up is a minute of ledger divergence.

---

## What console mode costs you

Deliberately chosen to start with, but be clear about the trade:

- **It dies with the window.** Closing it, logging out or rebooting stops the bot, and nothing
  restarts it. A reboot at 3am means the bot is down until somebody notices.
- **No crash recovery.** discord.py reconnects internally, so the process is fairly durable —
  but an unhandled crash or a rejected token exits and stays exited.
- **The token is a machine environment variable**, readable by every process on the box and
  visible in plaintext in the registry. On Linux it was a root-only file the service read
  before dropping privileges.

## Hardening, when you want it

**Supervision — Task Scheduler**, built in, no download. Create a task that runs
`run.cmd`, triggered *At startup*, with *Run whether user is logged on or not*, and under
Settings enable *If the task fails, restart every 1 minute*. The one field people leave blank
is **Start in** — set it to the `kit-app` folder, or a relative path in the config resolves
against `C:\Windows\System32` and the bot comes up with an empty ledger.

**Supervision — NSSM** if you want something closer to systemd: a real service with proper
restart handling and stdout redirection. Costs a third-party binary on the box.

**Token — DPAPI.** Encrypt it so only this account on this machine can read it:

```powershell
# once, as the account that will run the bot
$s = Read-Host -AsSecureString "token"
$s | ConvertFrom-SecureString | Set-Content C:\ProgramData\melonkit\token.dpapi

# in a launcher, before starting the bot
$sec = Get-Content C:\ProgramData\melonkit\token.dpapi | ConvertTo-SecureString
$env:MELONKIT_DISCORD_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
```

That is the closest Windows equivalent to the root-owned file systemd read on your behalf.

## Rolling back to Linux

Schema 6 refuses to open under an older build, but the code is the same on both hosts, so a
rollback is only ever a data move in the other direction: stop the Windows bot, copy the
ledger back (checkpoint first, same as above), `sudo systemctl enable --now melonkit-bot`.
Never both at once.

## Operational differences worth knowing

| | Linux (systemd) | Windows (console) |
|---|---|---|
| Logs | `journalctl -u melonkit-bot` | the file at `ops.log_path` |
| Start | `systemctl start melonkit-bot` | `run.cmd` |
| Stop | `systemctl stop melonkit-bot` | Ctrl-C |
| At boot | automatic | **not until you add a supervisor** |
| Token | root-only file, read before dropping privileges | machine environment variable |
| Ledger | `/var/lib/melonkit/`, `UMask=0027` | `C:\ProgramData\melonkit\`, ACL-restricted |
| One-shot scripts | `systemd-run --uid=ubuntu ...` | run them directly; there is no privilege drop to arrange |

The `deploy/` scripts written for Linux (`smoke_ticket.py`, `verify_buttons.py`,
`verify_thread_access.py`, `reset_tickets.py`) are plain Python and run here unchanged — call
them with the venv interpreter and `--config C:\ProgramData\melonkit\melonkit.json`. Ignore
the "run as the service user, never under sudo" notes in their headers; that hazard is a
root-owned ledger, which does not exist here.
