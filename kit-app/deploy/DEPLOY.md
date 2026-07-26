# Deploying the kit bot

Live on **ovh-2** (`158.69.219.29`, Ubuntu 25.04, Python 3.13.3), alongside the ARD stack. This
file is the record of how it got there, so it can be rebuilt without archaeology.

## Layout on the host

| path | what | owner |
|---|---|---|
| `/home/ubuntu/melon-kits` | the repo checkout | `ubuntu` |
| `/home/ubuntu/melon-kits/kit-app/.venv` | virtualenv, `discord.py` only | `ubuntu` |
| `/home/ubuntu/melon-kits/kit-app/melonkit.json` | config: guild/channel/role ids | `ubuntu` 640 |
| `/var/lib/melonkit/melonkit.sqlite3` | **the ledger** | `ubuntu`, dir 750 |
| `/etc/melonkit/env` | **the token, and nothing else** | `root` 600 |
| `/etc/systemd/system/melonkit-bot.service` | the unit | `root` 644 |

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
| `melonkit-bot.service` | the systemd unit, mirrored from what is installed |
| `setup_channels.py` | creates the three channels with the right overwrites and the six forum tags. Idempotent; prints the ids as JSON |
| `smoke_ticket.py` | drives a real ticket without a button press, for testing after a change. `--cleanup <id>` removes it |
| `reset_tickets.py` | clears ticket history for a test reset. Backs the ledger up first |

All three need the token, so run them through the `systemd-run --uid=ubuntu` wrapper below —
never under plain `sudo`, for the reason in the next section.

## Updating

```bash
ssh ubuntu@158.69.219.29
git -C ~/melon-kits pull                      # deploy key is stored in core.sshCommand
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

## Repo access

The repo is private, so the host has a **read-only deploy key** (`~/.ssh/melonkit_deploy`),
registered on the repo as *"ovh-2 deploy (read-only)"*. It is wired in per-repo rather than
globally, matching the `ard-server` convention:

```bash
git -C ~/melon-kits config core.sshCommand \
  'ssh -i ~/.ssh/melonkit_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
```

Read-only on purpose: the host has no reason to push, and a compromised VPS should not be
able to rewrite the repo.

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
  creating a venv, not by importing. Fix: `sudo apt-get install -y python3.13-venv`.
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
