# Operations

Running the kit app day to day, and the traps that cost real time getting it there.

If you are reading this to fix something broken, skip to [When something is
wrong](#when-something-is-wrong). If you are reading it because you changed something and it
behaved strangely, [Traps](#traps-that-cost-real-time) is probably where the answer is.

---

## Where everything is

Live on **ovh-2** (`158.69.219.29`), alongside the ARD stack. Full build record in
[kit-app/deploy/DEPLOY.md](../kit-app/deploy/DEPLOY.md).

| what | where |
|---|---|
| service | `melonkit-bot.service`, enabled at boot, `User=ubuntu` |
| code | `/home/ubuntu/melon-kits` (read-only deploy key) |
| virtualenv | `kit-app/.venv` — one dependency, `discord.py` |
| config | `kit-app/melonkit.json` — gitignored, so a pull never clobbers it |
| **ledger** | `/var/lib/melonkit/melonkit.sqlite3` — **outside the checkout on purpose** |
| **token** | `/etc/melonkit/env`, `root:600` — the only place it exists |
| logs | journald: `journalctl -u melonkit-bot` |

Three Discord channels, and the split between them is a privacy boundary rather than
housekeeping:

| channel | who sees it | holds |
|---|---|---|
| `#kit-requests` | **public** | the pinned panel; a **private thread** per ticket |
| `#kit-queue` | staff only | one forum post per ticket: card, decision, claim, delivery |
| `#kit-archive` | staff only, read-only | one transcript per finished ticket |

**Applicants never see a reviewer card.** It carries the ledger fan-out, the reviewer flag
list and the screening counts. Their thread gets a receipt, the outcome, and delivery
coordination — nothing else.

---

## Everyday tasks

Everything below runs on the host. Note the `systemd-run` wrapper on anything that needs the
token: it lets systemd read the root-only env file while the process still runs as `ubuntu`.
That is not fussiness — see [Never run bot.py as root](#never-run-botpy-as-root).

```bash
# a shorthand worth pasting into your shell first
KA=/home/ubuntu/melon-kits/kit-app
RUN="sudo systemd-run --uid=ubuntu --pipe --wait --collect --quiet
     --property=EnvironmentFile=/etc/melonkit/env
     --property=WorkingDirectory=$KA $KA/.venv/bin/python"
```

**Deploy a change**

```bash
git -C ~/melon-kits pull
$KA/.venv/bin/pip install -q -r $KA/requirements.txt   # only if requirements changed
sudo systemctl restart melonkit-bot
journalctl -u melonkit-bot -f
```

**Deploy a change that migrates the ledger.** Back it up first, and note that copying
`melonkit.sqlite3` on its own is **not** a backup: the database runs in WAL mode, so recent
commits can live entirely in the `-wal` file. Checkpoint first, or copy all three files.

```bash
$KA/.venv/bin/python -c "import sqlite3; \
  print(sqlite3.connect('/var/lib/melonkit/melonkit.sqlite3') \
        .execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())"
STAMP=$(date +%Y%m%d-%H%M%S); mkdir -p ~/melonkit-backups
for f in melonkit.sqlite3 melonkit.sqlite3-wal melonkit.sqlite3-shm; do
  [ -f /var/lib/melonkit/$f ] && cp -a /var/lib/melonkit/$f ~/melonkit-backups/$f.$STAMP
done
```

Then **rehearse the migration on the copy** before restarting the service — it runs in
`Store.__init__`, so opening the copy is the whole test:

```bash
cp ~/melonkit-backups/melonkit.sqlite3.$STAMP /tmp/rehearse.sqlite3
cd $KA && ./.venv/bin/python -c "import store; store.Store('/tmp/rehearse.sqlite3').close()"
```

A migration is **one-way**: the store refuses to open a ledger written by a newer schema rather
than risk mangling the kit history, so the old code will not start against a migrated file. The
backup is the rollback.

**Update the panel** — after editing any `panel` value in the config (the rescued counter, the
as-of date, the quoted response time), or any copy in `bot.py`:

```bash
$RUN bot.py --config melonkit.json --post-panel <PANEL_CHANNEL_ID>
```

This **edits the existing panel in place**, matched on the button's `custom_id`, so it keeps
its pin and its position. `/panel` in Discord does the same thing. You never need to delete
and re-post, and you should not — a stale panel with a live button is a confusing thing to
leave lying around.

**Create or re-sync the channels** — idempotent, safe to re-run:

```bash
$RUN deploy/setup_channels.py --config melonkit.json
```

It prints the three channel ids as JSON. It will report that overwrites could not be synced
unless the bot holds Manage Roles; that is expected and harmless once the channels exist —
see [Manage Roles is not Manage Channels](#manage-roles-is-not-manage-channels).

**Check the Discord layer before deploying** — the unit suite is stdlib-only and therefore
cannot import `bot.py` at all, so nothing else covers modals, buttons, custom_id templates or
the `/setup` pickers. This does, without connecting:

```bash
MELONKIT_DISCORD_TOKEN=x $KA/.venv/bin/python deploy/verify_components.py
```

Exits non-zero on the first problem. It has caught a real one already: `/setup`'s channel picker
offered **media** channels for the reviewer queue, where Discord requires an attachment on every
post — so every ticket would have failed to post and auto-cancelled.

**Reset ticket history** (test data only — it takes a backup first):

```bash
$RUN deploy/reset_tickets.py --config melonkit.json --dry-run
$RUN deploy/reset_tickets.py --config melonkit.json --yes --delete-discord
```

Reviewer flags survive by default: they are accumulated human knowledge about accounts, not
ticket data. `--flags-too` drops them as well.

**Drive a ticket without a button press** — for testing after a change:

```bash
$RUN deploy/smoke_ticket.py --config melonkit.json --name <MCNAME> --user <DISCORD_ID>
$RUN deploy/smoke_ticket.py --config melonkit.json --cleanup <TICKET_ID>
```

It performs the operations a real press performs — private thread plus `add_user`, a card
from live 2b2t data, a forum post with tags and an attachment — and the buttons it posts are
the real ones, so pressing Approve on its post runs the rest of the lifecycle for real.

**Read the ledger**

```bash
sqlite3 /var/lib/melonkit/melonkit.sqlite3 \
  'select id, mc_name, status, datetime(created_at,"unixepoch") from tickets order by id desc limit 20'
```

---

## When something is wrong

| symptom | almost certainly | fix |
|---|---|---|
| "You already have a request open" but the thread is gone | nothing — this self-heals now | press the button again; it auto-closes the orphan first |
| A ticket is stuck open and nobody will decide it | needs closing, not fixing | `/close <ticket> <reason>`. The applicant can also close their own |
| A delivery is claimed by someone who went quiet | stale claim | `/unclaim <kit>` — a reviewer can release anyone's claim |
| Button click does nothing / "This interaction failed" | an exception in a handler | `journalctl -u melonkit-bot -n 50`. Handlers log a traceback now instead of failing silently |
| Reviewer card says "could not be retrieved" | `api.2b2t.vc` rate-limited or down | nothing to do. The limit is **5 req/s shared across every caller on the internet**, so it is often not your traffic |
| Chat section says "No lexicon loaded" | expected | that is correct until you populate a lexicon; see [chat-screening.md](chat-screening.md) |
| Transcript conversation says "not captured" | `capture_thread_messages` is off | set it true. **No privileged intent needed** |
| Service restart-looping | bad or revoked token | `journalctl` will say `Discord rejected the token`. Five tries then it stops and waits for a human, deliberately |
| First real ticket fails with "readonly database" | something ran as root | see [Never run bot.py as root](#never-run-botpy-as-root) |

Health check, in one line:

```bash
systemctl is-active melonkit-bot && journalctl -u melonkit-bot -n 20 --no-pager
```

---

## Traps that cost real time

Every one of these was hit for real. They are recorded because none of them is guessable and
several failed *silently* or with a misleading error.

### SQLite objects are bound to the thread that made them

**Symptom:** a ticket row appears in the ledger, then three seconds later
`ERROR request modal failed err=ProgrammingError`, and the applicant sees nothing. **Every**
button press fails.

**Cause:** blocking work — the 2b2t requests, and the ledger reads inside `card.gather` —
runs in `loop.run_in_executor`, i.e. a worker thread. A single shared `sqlite3` connection
raises *"SQLite objects created in a thread can only be used in that same thread"*.

**Why it was not obvious:** the smoke test was green throughout, because it called
`card.gather` directly on the main thread. A test that does not reproduce the threading shape
proves nothing about it. The store now keeps one connection per thread, and the smoke test
goes through an executor deliberately.

### MESSAGE_CONTENT does not gate REST history

**Symptom:** transcripts said the conversation could not be captured, and enabling capture
looked like it required a privileged intent.

**Cause:** it does not. `MESSAGE_CONTENT` gates message content in **gateway events**. REST
history fetches (`GET /channels/{id}/messages`) are governed by **Read Message History**,
which the bot already has in its own threads. Verified against real messages.

**Why it matters:** the code originally *requested* the intent when capture was enabled, which
would have made the bot refuse to start until someone toggled the Developer Portal — a hard
failure in exchange for nothing.

**Testing note:** system messages (type 7 join, type 19 reply-without-text) have legitimately
empty content, so testing against them is inconclusive. You need real type-0 human text.

### Pin Messages is a separate permission from Manage Messages

**Symptom:** `403 / code 50013` on `message.pin()` while `permissions_for(me).manage_messages`
returns `True`, guild MFA is disabled, and both the old and new REST pin endpoints refuse.

**Cause:** Discord split pinning out into its own **Pin Messages** permission
(`discord.Permissions.pin_messages`).

It is optional here — `@everyone` cannot post in `#kit-requests`, so the panel stays the newest
message whether or not it is pinned.

### Manage Roles is not Manage Channels

**Symptom:** re-running `setup_channels.py` to add one forum tag returns `403`, even though the
bot demonstrably has Manage Channels.

**Cause:** editing channel **permission overwrites** requires **Manage Roles** ("Manage
Permissions" on a channel). Manage Channels is not enough — and confusingly, *creating* a
channel with overwrites **is** allowed while editing them later is not.

The script now does the topic and the overwrites as separate calls, each tolerating its own
`Forbidden`, so the thing it cannot do no longer blocks the thing it can.

### Never run bot.py as root

**Symptom:** the bot connects, looks completely healthy, and then the first real ticket dies
with `attempt to write a readonly database`.

**Cause:** `KitBot.__init__` opens the ledger, so **any** invocation touches it — including
`--post-panel` and `--print-config`. Run one under `sudo` (tempting, because the token is in a
root-only file) and the database ends up `root`-owned, after which the service, running as
`ubuntu`, can read but not write it.

**Why it does not fail at startup:** `CREATE TABLE IF NOT EXISTS` on an existing schema writes
nothing, so opening it succeeds.

Use the `systemd-run --uid=ubuntu` wrapper at the top of this document. If it has already
happened:

```bash
sudo systemctl stop melonkit-bot
sudo chown ubuntu:ubuntu /var/lib/melonkit/melonkit.sqlite3*
sudo chmod 640 /var/lib/melonkit/melonkit.sqlite3*
sudo systemctl start melonkit-bot
```

**Verify by writing, not by reading** — a read succeeds either way.

### Acknowledge an interaction before any network call

Discord gives **3 seconds** for an initial interaction response. `handle_decision` used to do
a `fetch_channel`, then message and thread edits, before responding. Blowing the deadline shows
*"This interaction failed"* **after** the decision has already been recorded, which invites the
reviewer to press again.

Combined with the race below, a second press meant a second kit. Always `defer()` first.

### Every command listed twice

**Symptom:** the command picker shows `/setup`, `/setup`, `/panel`, `/panel` — every command
duplicated, in one guild only.

**Cause:** syncing the same tree both globally *and* guild-scoped. Discord keeps global and
per-guild command registrations in **separate namespaces and does not dedupe between them**, so
a guild that has both lists both. Startup used to do `copy_global_to(guild=...)` for the home
guild so edits appeared there without waiting on global propagation, which bought a few seconds
and cost a doubled command list.

**Fix, and the part that is easy to get wrong:** deleting the copy code is not enough. A
guild-scoped registration lives on Discord's side until something overwrites it, so a bot that
ever ran the old code keeps showing doubles forever, through any number of restarts and
redeploys. What actually removes them is pushing an **empty** command list to that guild:

```python
tree.clear_commands(guild=discord.Object(id=home))
await tree.sync(guild=discord.Object(id=home))
```

That now runs on every start rather than as a one-off, so any install that ever ran an affected
version repairs itself. It is idempotent and costs one request.

**Diagnosing it:** `GET /applications/{app}/guilds/{guild}/commands` returning a non-empty list
is the duplicate set. The global list is `GET /applications/{app}/commands`.

### A channel-type annotation is wider than it reads

`Optional[discord.TextChannel]` on a slash-command parameter makes Discord offer **text and
announcement** channels; `discord.ForumChannel` offers **forum and media**. Neither extra is
something an admin would notice picking.

They are not equivalent. An announcement channel supports only announcement threads, which are
public — so the private-thread-per-ticket design silently degrades to no thread at all. A media
channel **requires an attachment on every post**, and the reviewer card is an embed, so every
ticket would fail to post and get auto-cancelled. `/setup` warns about the first and refuses the
second.

Check what a picker will really offer with `deploy/verify_components.py` rather than reading the
annotation and assuming.

### A modal has no deadline, but discord.py gives it one anyway

Discord puts **no** time limit on submitting a modal — the dialog sits open as long as somebody
keeps typing. discord.py's own view timeout does, and when it expires the submission dispatches
to nothing: *"This interaction failed"*, and everything they wrote is gone.

Every modal here shipped with `timeout=600`. That is fine for a two-field rescue form and wrong
for the funding one: an applicant assembling a materials list is checking their stock in game
while the form sits open, so ten minutes is a normal answer rather than an abandoned one. All
four modals now pass `timeout=None`, which is `Modal.__init__`'s own default.

### An ephemeral view is force-timed-out at 15 minutes

`InteractionResponse.send_message` contains `if ephemeral and view.timeout is None: view.timeout
= 15 * 60.0`. **There is no way to opt out.** So a paginator that keeps its page number in the
view instance is dead a quarter of an hour after it opens, with no restart involved — and the
failure is silent: view dispatch finds nothing registered and simply returns, no exception and no
log line, on a button that still looks live.

This is why the chat pager encodes the target page in the Prev/Next `custom_id` instead. Anything
paged or stateful sent ephemerally has to be stateless the same way.

### A plain button among dynamic ones unregisters them globally

Related, and nastier. When a view holding `DynamicItem`s is removed — which the forced 15-minute
ephemeral timeout above *will* do — discord.py pops each of their compiled templates out of the
process-global registry, but only when the view also has a dispatchable child that is **not** a
DynamicItem.

So adding an innocent disabled "Page 3/5" indicator button to the pager would, fifteen minutes
after the first press, break Prev/Next **for every reviewer in every guild** until the next
restart. Every child of that view is a DynamicItem, and the page counter lives in the embed
footer. Do not add an ordinary button to it.

### Components are replaced wholesale; attachments are not

`message.edit(...)` leaves existing attachments alone when you omit `attachments=`, but a view
you pass replaces the previous one entirely. The chat log used to be a `.txt` on the queue post,
so it survived approve → claim → deliver → archive for free. As a button it does not: every
lifecycle edit has to re-add it, which is what `_with_chat` is for.

If chat vanishes from a decided ticket, look for a `qmsg.edit(view=...)` that was not routed
through that helper. `on_thread_delete` was the one that got missed.

### applied_tags is replace, not merge

`thread.edit(applied_tags=[...])` sets the complete list. `_set_queue_state` used to pass only
the new lifecycle tag, which was harmless while lifecycle was the only thing tagged — and would
have silently deleted the request-type tag on the first Approve.

It now keeps every tag that is not in `QUEUE_TAGS` and puts the lifecycle tag **first**, because
a post is capped at 5 applied tags and the lifecycle tag must never be the one truncated.

### Read-then-write is not a lock

`record_decision` used to check `if status == open` and then update. Two reviewers pressing
Approve at the same instant both passed, both recorded a kit, and the applicant's 21-day
cooldown was burned twice for one request.

It is now a conditional `UPDATE ... WHERE status='open'` inside `BEGIN IMMEDIATE`, returning
whether the caller won — the same shape the Claim button already used. **Any state transition
two people can trigger simultaneously needs to be conditional at the database.**

### Three separate routes to locking an applicant out

The panel pre-check counts *open* tickets, so anything that leaves a ticket open forever bars
that person from **ever** requesting again — silently. Given the kits are disposable and
under-helping is the expensive direction, this was the worst failure the app had. All three are
now closed:

1. **No way to close an undecided ticket** → `/close`, open to the applicant as well as staff.
2. **The applicant deletes their thread** → `on_thread_delete` closes it, and the button
   pre-check re-checks, because a deletion while the bot is down fires no event.
3. **The queue card fails to post** → auto-closed, since no reviewer would ever see it.

### Forum posts cannot be private

`ForumChannel.create_thread` has no `type` / `private` / `invitable` parameter, and `Thread`
has no `overwrites` and no `set_permissions` — a thread's visibility strictly follows its
parent channel. So there is **no** way to admit one applicant to one post in a staff forum
without giving them read access to every other ticket. That is why tickets live as private
threads off a text channel, and why `--post-panel` refuses a forum outright.

### The server owner bypasses every overwrite

You cannot verify that non-staff are locked out of `#kit-queue` using the owner account —
owners hold all permissions unconditionally, so `permissions_for(owner).view_channel` is always
`True`. The meaningful evidence is `@everyone → False` plus the overwrite rows. A real check
needs a second, non-staff account.

### Don't hold an SSH session open waiting for a long job

A full mining pass over the 6.7M-row dump takes about thirteen minutes. Wrapping it in a
watcher —

```bash
ssh ovh-2 'nohup python3 tools/mine.py test ... &
           until [ -s report.txt ]; do sleep 15; done; cat report.txt'
```

— reliably dies with `Connection reset by peer` and **exit 255**. The job itself is fine: it is
detached under `nohup` and always finishes. What dies is the watching client, and the exit code
you get back is the watcher's, not the work's. It reads like a failed job and is not one.

Start it and come back instead:

```bash
ssh ovh-2 'cd ~/melon-kits/kit-app && nohup python3 tools/mine.py test ... > mine.log 2>&1 &'
# ...later, a separate short connection:
ssh ovh-2 'tr "\r" "\n" < ~/vcdb-2025/mine.log | tail -1'   # progress
ssh ovh-2 'sed -n "1,12p" ~/vcdb-2025/report.txt'           # result
```

### Host quirks on ovh-2

- **`python3-venv` is not installed by default on Ubuntu 25.04, and `python3 -c "import venv"`
  succeeds anyway** — the module is present, `ensurepip` is not. Test by actually creating a
  venv. Fix: `sudo apt-get install -y python3.13-venv`.
- **`sudo` here refuses `DEBIAN_FRONTEND`.** Use `apt-get -y -qq < /dev/null`.
- discord.py logs two startup warnings about `PyNaCl`/`davey` and voice. Expected and
  irrelevant.

### Gitignore patterns need the exact suffix

`*.sqlite3` does **not** match `melonkit.sqlite3-wal`, `-shm`, or
`melonkit.sqlite3.bak-1785036711`. Those are all full or partial copies of a ledger containing
chat. Each needs its own pattern, and they all have one now.
