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
