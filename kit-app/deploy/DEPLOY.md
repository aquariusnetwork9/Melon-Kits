# Deploying the kit bot

## For a new host, use the installer

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Melon-Kits/main/install.sh | sudo bash
```

It asks for the bot token and nothing else, then prints the invite link and the one remaining
step (`/setup`). Re-run the same command to upgrade — it never overwrites the token, the config
or the ledger. `--help` lists the flags; `--uninstall` removes the service and deliberately
keeps the data.

Everything in the rest of this file is what the installer *does*, and is worth reading before
changing it. The paths below are the installer's defaults; **ovh-2 predates it** and runs the
same thing at different paths as the `ubuntu` user, which is why the unit is a template.

## Layout on the host

| path | what | owner |
|---|---|---|
| `/opt/melon-kits` | the repo checkout | `melonkit` |
| `/opt/melon-kits/kit-app/.venv` | virtualenv, `discord.py` only | `melonkit` |
| `/opt/melon-kits/kit-app/melonkit.json` | config: paths and user agent | `melonkit` 640 |
| `/var/lib/melonkit/melonkit.sqlite3` | **the ledger** | `melonkit`, dir 750 |
| `/etc/melonkit/env` | **the token, and nothing else** | `root` 600 |
| `/etc/systemd/system/melonkit-bot.service` | the unit, rendered from `melonkit-bot.service.in` | `root` 644 |

On **ovh-2** (`158.69.219.29`, Ubuntu 25.04, Python 3.13.3) the same layout lives under
`/home/ubuntu/melon-kits` and runs as `ubuntu`.

**Channel and role ids are not in the config file.** `/setup` writes them to the ledger, per
guild, which is why one config works for any server.

Two separations that are deliberate:

- **The ledger lives outside the checkout.** `git pull`, a re-clone, or a botched deploy
  cannot touch kit history. `store.path` in the config points at `/var/lib/melonkit`.
- **The token is not in the config file.** It is only ever `MELONKIT_DISCORD_TOKEN` in a
  root-owned 600 file that systemd reads before dropping to `ubuntu`, so the process user
  cannot read it off disk and it cannot be committed. Same pattern as
  `highway-discord-bot`.

## The scripts in here

| script | what it does |
|---|---|
| `melonkit-bot.service.in` | the systemd unit **template**. `install.sh` substitutes the user and paths and writes the result. Edit this, never the installed copy — a re-run overwrites it |
| `setup_channels.py` | creates the three channels with the right overwrites and the six forum tags. Idempotent; prints the ids as JSON |
| `smoke_ticket.py` | drives a real ticket without a button press, for testing after a change. `--cleanup <id>` removes it |
| `reset_tickets.py` | clears ticket history for a test reset. Backs the ledger up first |

All three need the token, so run them through the `systemd-run --uid=ubuntu` wrapper below —
never under plain `sudo`, for the reason in the next section.

## Updating

Re-run the installer. On ovh-2, whose install predates it, it is still done by hand:

```bash
ssh ubuntu@158.69.219.29
git -C ~/melon-kits pull
~/melon-kits/kit-app/.venv/bin/pip install -q -r ~/melon-kits/kit-app/requirements.txt
sudo systemctl restart melonkit-bot
journalctl -u melonkit-bot -f
```

`melonkit.json` and `.venv` are both gitignored, so a pull never clobbers config or the
virtualenv.

## Setting the token

```bash
sudo nano /etc/melonkit/env      # MELONKIT_DISCORD_TOKEN=...
sudo systemctl restart melonkit-bot
```

Or re-run the installer with `--token`, which also resets the start-limit counter — see below.

## The start limit latches

The unit gives up after five failed starts in five minutes, on purpose: a crash-loop against
Discord's auth endpoint is how an application gets rate-limited. The consequence is that after a
bad token, **systemd refuses a plain `systemctl restart`** until the counter is cleared:

```bash
sudo systemctl reset-failed melonkit-bot
sudo systemctl restart melonkit-bot
```

The installer does this for you, which matters because "mistype the token, then re-run with the
right one" is the single most likely recovery path there is.

## Repo access

The repo is **public**, so a host needs no credentials to clone or pull it. (An earlier version
of this file described a read-only deploy key for a private repo; that is no longer the case.)
Nothing secret is in it — the token lives only in `/etc/melonkit/env`, and the shipped
`lexicon.example.json` deliberately has empty `slur` and `profanity` lists.

## Sandboxing

The unit runs under `ProtectSystem=strict` and `ProtectHome=read-only`, with exactly one
writable path (`/var/lib/melonkit`). Consequences worth knowing before editing the unit:

- **The checkout is read-only at runtime.** `PYTHONDONTWRITEBYTECODE=1` is set because
  bytecode caching would otherwise fail on every import.
- **`UMask=0027`.** The ledger holds redacted chat plus every decision and its reason;
  without this it lands 644 and world-readable. It has to be a umask rather than a `chmod`
  because SQLite creates `-wal` and `-shm` alongside the database from the process umask, and
  because a deleted database is recreated fresh.
- **`StartLimitBurst=5` / `StartLimitIntervalSec=300`.** With a bad or revoked token the bot
  exits 3 immediately; without a start limit `Restart=on-failure` would crash-loop against
  Discord's auth endpoint, which is a good way to get rate-limited at the application level.
  Five tries then stop and wait for a human.

Verify any edit with `systemd-analyze verify /etc/systemd/system/melonkit-bot.service`.

## Never run bot.py as root

`KitBot.__init__` opens the ledger, so **any** invocation creates or touches
`/var/lib/melonkit/melonkit.sqlite3` — including `--post-panel` and `--print-config`. Run one
of those under `sudo` and the database ends up `root`-owned, at which point the service (which
runs as `ubuntu`) can read it but not write, and the very first ticket fails with
`attempt to write a readonly database`. It does not fail at startup, because
`CREATE TABLE IF NOT EXISTS` on an existing schema writes nothing — so the bot connects to
Discord looking perfectly healthy and breaks on first use.

The token lives in a root-only file, which is what makes this tempting. Use `systemd-run` so
systemd reads the file as root and the process still runs as `ubuntu`:

```bash
sudo systemd-run --uid=ubuntu --pipe --wait --collect \
  --property=EnvironmentFile=/etc/melonkit/env \
  --property=WorkingDirectory=/home/ubuntu/melon-kits/kit-app \
  /home/ubuntu/melon-kits/kit-app/.venv/bin/python bot.py --config melonkit.json --post-panel <CHANNEL_ID>
```

If it has already happened:

```bash
sudo systemctl stop melonkit-bot
sudo chown ubuntu:ubuntu /var/lib/melonkit/melonkit.sqlite3*
sudo chmod 640 /var/lib/melonkit/melonkit.sqlite3*
sudo systemctl start melonkit-bot
```

Verify by actually writing, not by reading — a read succeeds either way.

## Pinning needs its own permission

Discord split pinning out of Manage Messages into a separate **`Pin Messages`** permission, so
a bot holding Manage Messages still gets `403 / 50013` on a pin while every client-side
permission check says it should work. `discord.py` exposes it as `Permissions.pin_messages`.

It is optional for this app: `@everyone` cannot post in the requests channel, so the panel is
permanently the newest message whether or not it is pinned. `setup_channels.py` only adds it to
the bot's overwrite when the bot actually holds it guild-wide, because an overwrite cannot
grant a permission the granter lacks and including it unconditionally would fail the whole
channel create.

## Host gotchas hit during setup

- **`python3-venv` is not installed by default on Ubuntu 25.04**, and `python3 -c "import
  venv"` *succeeds* anyway — the module is present, `ensurepip` is not. Check by actually
  creating a venv, not by importing. Fix: `sudo apt-get install -y python3.13-venv`. The
  installer does exactly this: it tries to create one, and on failure installs
  `python3.N-venv` and retries.
- **An unknown key in `melonkit.json` is a hard startup error**, by design, so that a typo
  cannot silently do nothing. That includes a `"_comment"` key — there is no way to annotate the
  config from inside it. The installer therefore writes a bare config and validates it (by
  importing `config`, which touches neither the ledger nor the token) *before* starting the
  service, because otherwise the unit flaps five times and the reason ends up buried.
- **`raw.githubusercontent.com` caches for a few minutes.** Right after pushing a change to
  `install.sh`, a fetch can still return the previous version. Use
  `https://api.github.com/repos/aquariusnetwork9/Melon-Kits/contents/install.sh?ref=main` with
  `Accept: application/vnd.github.raw` to bypass it when testing.
- **`set -o pipefail` plus `grep -q` inverts the test.** A matching `grep -q` exits early, the
  upstream command takes SIGPIPE, and the pipeline reports failure — so the branch that matched
  is the one that does not fire. Read into a variable and match that instead.
- **sudo here refuses `DEBIAN_FRONTEND`** (`sorry, you are not allowed to set the following
  environment variables`). `apt-get -y -qq < /dev/null` is non-interactive enough.
- discord.py logs two warnings at startup about `PyNaCl`/`davey` and voice support. Expected
  and irrelevant — this bot has nothing to do with voice.

## Verifying without a real token

Setting `MELONKIT_DISCORD_TOKEN` to an obviously invalid value and starting the service is a
useful end-to-end check: the store is opened during `KitBot.__init__`, *before* the Discord
login, so reaching `Discord rejected the token in MELONKIT_DISCORD_TOKEN.` and exit 3 proves
the venv, config, paths and — importantly — the sandbox's writable path all work. Anything
earlier in the chain fails differently and more loudly.
