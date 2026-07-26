# kit-app

The Discord app: a pinned panel with a button, a private thread per request, a reviewer card
built from public 2b2t history, a claimable dispatch, and an archived transcript when it is
done.

This file is how to set it up. For **running** it — everyday tasks, a symptom→fix table, and
every trap that cost real time — see [../docs/operations.md](../docs/operations.md). Design and
rationale are in [../docs/kit-request-flow.md](../docs/kit-request-flow.md) and
[../docs/reviewing.md](../docs/reviewing.md).

---

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Linux: .venv/bin/pip
cp melonkit.example.json melonkit.json             # then fill in the ids
export MELONKIT_DISCORD_TOKEN='...'                # Windows: $env:MELONKIT_DISCORD_TOKEN
python bot.py --config melonkit.json --print-config   # check it before connecting
python bot.py --config melonkit.json
```

**One dependency, `discord.py>=2.4`, and only `bot.py` imports it.** Every other module here
is standard library only, so the whole test suite runs with nothing installed:

```bash
python -m unittest discover -s tests -t .          # 132 tests, no network, no discord.py
```

That split is deliberate. The logic worth testing — cooldowns, redaction, screening, card
assembly — has no Discord in it, so none of it needs a mocked gateway to test.

It has one sharp edge, though, and it bit for real: a test that does not reproduce the
*threading* shape of the handler proves nothing about it. The card builder runs in
`run_in_executor`, and a smoke test that called it on the main thread stayed green while every
real button press died on a cross-thread SQLite error. `deploy/smoke_ticket.py` now goes through
an executor deliberately.

---

## Discord setup

In the [developer portal](https://discord.com/developers/applications): create an
application, add a bot, copy the token into `MELONKIT_DISCORD_TOKEN`.

**No privileged intents are required — none, for anything.** Do not enable anything on the Bot
page. That includes transcript conversation capture: `MESSAGE_CONTENT` gates content in
*gateway events*, not in REST history fetches, which only need Read Message History. Verified
against real messages, because the docs read as though it covers both.

Invite it with `bot` + `applications.commands` and permissions **2252194950933520** — that is
the running set plus **Manage Channels**, which `/setup` needs to create the three channels.
Once a server is set up you can drop back to **2252194950925312**; nothing at runtime uses
Manage Channels.

**`Pin Messages` is a separate permission from `Manage Messages`.** Discord split pinning out
into its own permission bit, so a bot holding Manage Messages still gets `403 / 50013` when it
tries to pin — the permission check passes and the API refuses anyway. It is optional here:
`@everyone` cannot post in the requests channel, so the panel stays the newest message
permanently whether or not it is pinned.

### Installing it on a server

**One bot serves any number of servers.** Invite it, then someone with **Manage Server** runs:

```
/setup reviewer_role:@reviewer delivery_role:@delivery
```

That is the whole install. It creates `#kit-requests`, `#kit-queue` and `#kit-archive` with
the right permissions, adds the six lifecycle tags, stores the configuration for that server,
and posts the panel. It is idempotent — re-run it any time; it reuses whatever exists.

**Each server's tickets, cooldowns, flags and archive are its own.** Nothing crosses between
them, including reviewer flags. The one shared resource is the `api.2b2t.vc` rate limit,
which is 5 requests/second globally across every caller on the internet, so many busy servers
queue behind each other.

`Manage Channels` is needed **only** by `/setup` and can be removed afterwards.
`deploy/setup_channels.py` does the same job from the command line if you prefer.

Channel and role ids are **not** in `melonkit.json` — they live in the database, per server.
The file holds only what is the same everywhere:

| key | what it is |
|---|---|
| `discord.home_guild_id` | optional. Your own server, which additionally gets a guild-scoped command copy so command edits appear instantly there instead of waiting on Discord's global propagation. It does **not** make the bot single-guild |
| `discord.capture_thread_messages` | include the applicant conversation in transcripts. Needs no privileged intent |
| `policy.*` | cooldown days, how much chat to show, what counts as "new" or "just wiped" |
| `panel.rescued_count`, `panel.rescued_as_of`, `panel.response_time` | the numbers in the panel copy — edit, then `/panel` |
| `vc.user_agent` | **always set this** to something with a real contact in it |

Upgrading from the single-guild version needs no action: the old `guild_id` key is still
accepted, and on first start the channel and role ids in the file are adopted into that
server's stored config and its existing rows are stamped with it.

**Pin the panel that `/setup` posts.** The button's `custom_id` is static, so it keeps working across
restarts, redeploys and version bumps indefinitely. Running `/panel` again **edits that
message in place** rather than posting a second one, so changing the copy or the rescued
counter keeps the pin and the position — you never need to delete and re-post.

---

## Commands

| command | who | what |
|---|---|---|
| `/setup <reviewer_role> [delivery_role] [category] [requests_channel] [queue_channel] [archive_channel]` | **Manage Server** | install on this server: makes the three channels and the tags, stores the config, posts the panel. Idempotent |
| `/roles` | **Manage Server** | set which roles review and which deliver — **up to 8 each**, via role-pickers |
| `/panel` | reviewer | re-post or update the panel. Edits an existing one in place |
| `/lookup <name>` | reviewer | a reviewer card with no ticket attached — for answering "would this even pass" |
| `/flag <name> <kind> <note>` | reviewer | mark an account: known alt, do not serve, or a note. Resolves to a UUID so it survives a rename |
| `/unflag <id>` | reviewer | clear one |
| `/ledger <name>` | reviewer | kit history, cooldown state and flags for an account |
| `/close <ticket> <reason>` | reviewer **or the applicant** | close a ticket with no decision — the applicant went quiet, withdrew, or it was a duplicate |
| `/unclaim <kit>` | the claimer **or** a reviewer | hand a delivery back to the pool. The ticket stays approved; the kit is still owed |
| `/flagline <ticket> <lines>` | reviewer | label chat lines you objected to, e.g. `3,7,12` |

`/flagline` looks like a nicety and is the most valuable command here. See
**Instrumentation** below.

**`/close` is not optional housekeeping.** The panel's pre-check counts *open* tickets, so an
undecided ticket bars its applicant from ever requesting again. Without a way to close one
that is a silent, permanent lockout — and given the kits are disposable, under-helping is the
expensive direction. It is open to the applicant as well as to reviewers, because someone
withdrawing their own request only frees their own slot and shouldn't need to find staff.
Closing records no kit, so it never burns the 21-day cooldown.

---

## Three channels, split by audience

| channel | type | who can see it | holds |
|---|---|---|---|
| `#kit-requests` | **text**, public | everyone | the pinned panel, and a **private thread per ticket** — the applicant ↔ staff conversation |
| `#kit-queue` | **forum**, staff only | reviewer + delivery | one post per ticket: the reviewer card, the decision, the claim, the delivery. Tags carry the state |
| `#kit-archive` | **text**, staff only, read-only | reviewer + delivery | one transcript per finished ticket, with the chat log attached |

**Names are the default, not a requirement.** `/setup` will use channels the server already has
if you name them, which is the point on a server with a help channel people already know to go
to:

```
/setup reviewer_role:@Staff delivery_role:@Couriers
       category:Support                    ← anything I create goes in here
       requests_channel:#support-tickets   ← the panel goes here instead of a new channel
       queue_channel:#staff-review         ← optional; a forum, or a text channel
       archive_channel:#ticket-logs
```

**More than one role of each kind: `/roles`.** `/setup` takes one reviewer and one delivery role
because it needs *a* reviewer to exist before anything works. Servers where three roles review
and two deliver use `/roles` afterwards, which shows what is set and replaces the list from a
role-picker — up to **8 each**. Removing a role means deselecting it.

Two things to know about it. Whatever is selected *replaces* the list, so it is also how you
clear one; with no reviewer role set, anyone with Manage Server reviews, and with no delivery
role set, anyone can claim. And **adding a role to the list does not give it channel access** —
those are genuinely separate things, the ledger deciding who may press Approve and the channel
overwrites deciding who can see the post to press it on. `/roles` tries to grant it and names
any channel it could not, because editing overwrites needs Manage Roles, which this bot does not
ask for. A configured reviewer who cannot see the queue is the worst version of this failure:
they believe they are a reviewer and simply never see a ticket.

Everything past `reviewer_role` is optional, so the one-argument install is unchanged. Two
things follow from adopting rather than creating:

- **An adopted channel keeps its own permissions.** Setup does not touch them — partly because
  reconfiguring a channel that is in use for something else is rude, and partly because editing
  overwrites needs Manage Roles, which this bot deliberately never asks for. Instead the reply
  reports what it found and what will follow: if the channel lets everyone post, it says the
  panel will get pushed up as people talk (it stays pinned; `/panel` puts it back at the
  bottom); if the bot can't create private threads there, it says applicants will get no thread.
- **Manage Channels is only needed to make one.** Hand over all three and setup no longer asks
  for it at all — unless the queue is a forum, since adding tags needs it too.

The channel picker Discord builds from those options is wider than it looks: it offers
**announcement** channels wherever a text channel is accepted, and **media** channels wherever a
forum is. An announcement channel cannot have private threads, so setup warns. A media channel
requires an attachment on every post, so a reviewer card could never post there at all — setup
refuses that one outright rather than letting every ticket auto-cancel.

**The applicant never sees the reviewer card.** Their thread gets a receipt, the outcome, and
delivery coordination — nothing else. The card carries the ledger fan-out, the reviewer flag
list and the screening counts; showing someone the exact criteria applied to them exposes
notes written about them and teaches them how to game the next request. For the same reason a
decline tells the applicant the outcome but **not** the internal reason, which is written for
whoever reads the ledger in a year. A reviewer who wants to say more can just type in the
thread.

**The panel channel cannot be a forum, and this is a Discord limit rather than a preference.**
`ForumChannel.create_thread` has no `type`/`private`/`invitable` parameter — forum posts are
always public threads — and `Thread` has no `overwrites` and no `set_permissions`, so a
thread's visibility strictly follows its parent channel. There is therefore no way to let one
applicant into one post without letting them read every other ticket. `--post-panel` refuses
a forum with that explanation rather than posting something broken.

The queue forum uses six tags, matched case-insensitively: `awaiting review`, `approved`,
`declined`, `claimed`, `delivered`, `closed`. Missing ones are skipped, so a forum with no tags
still works — you just lose the filtering. Rename or restyle them freely; only the names are
matched.

## The archive

Every ticket that finishes — delivered, declined, closed, or auto-closed because its thread was
deleted — posts one transcript to `#kit-archive`: a compact three-line embed plus a `.txt`
attachment holding the full decision history, every chat line shown to the reviewer
(coordinate-redacted), and the applicant ↔ staff conversation.

Two design points worth knowing:

- **It is built from the ledger, not by scraping Discord**, so it still works after the thread
  and the queue post have been deleted — which is exactly the situation it exists for.
- **The embed is deliberately three dense lines, not stacked fields.** Discord gives every
  non-inline field its own row plus a heading, which ran to ~18 rendered lines per ticket and
  made the archive unscrollable. The attachment is the authoritative copy; the message only has
  to be enough to scan and to search. The colour stripe stays because green/red/grey is the
  fastest outcome cue there is and costs no height.

Conversation capture is `discord.capture_thread_messages` (on by default) and needs **no
privileged intent**. If Discord ever changes that, the transcript will say the conversation was
unreadable rather than recording a wall of blank lines.

## The flow, and why it is in this order

1. **Button press → pre-checks → *then* the form.** Open-ticket and cooldown checks run
   before the modal opens, against local SQLite, comfortably inside Discord's 3-second
   deadline. Making somebody fill in a form and only then telling them they were never
   eligible is the one avoidable bad experience in this flow.

   The panel has **two** buttons — rescue kit and project funding — rather than one form with a
   type selector inside it, and that is a platform constraint rather than a preference: a modal
   is submitted whole, so it cannot change its own questions once somebody picks a type. Asking
   a builder "anything we should know?" and a rescue applicant "how big is this?" in the same
   box makes both answers unusable. The cooldown pre-check is per kind, so being funded for a
   build does not stop you asking for a rescue kit when you die; the **one-open-ticket rule is
   deliberately not** per kind, because two open tickets is two threads and two cards for one
   person, and being stuck open is the most expensive failure this app has.
2. **Modal submit → deferred.** Name resolution plus three API calls takes a few seconds, so
   the interaction is acknowledged immediately and the work continues behind the 15-minute
   follow-up token.
3. **A private thread** for the applicant, with a receipt in it. If thread creation fails —
   the guild's ~1000 active-thread ceiling, or missing permissions — the ticket still goes to
   the queue. Losing the request would be worse than losing the thread.
4. **A queue post** with the card and Approve / Decline, tagged `awaiting review`. If posting it
   fails, the ticket is **auto-closed** rather than left open — nobody would ever see it, and an
   open ticket bars the applicant from trying again.
5. **Approve / Decline.** Both defer before doing any network work, because the 3-second
   deadline is shorter than a `fetch_channel` plus two edits, and blowing it shows "interaction
   failed" *after* the decision was recorded. Decline requires a reason; it is what makes the
   ledger legible a year later, and an optional field is an empty field.
6. **Approval turns the same post into the dispatch** — retagged `approved`, Claim button
   attached, card left in place. One post ends up being the entire record of a ticket rather
   than a card in one channel and an ember in another. The applicant's thread simultaneously
   gets a **Set meeting coordinates** button; whatever they enter appears on the queue post and
   in the claim confirmation, so the runner does not have to ask.
7. **Claim → Mark delivered**, then the post retags, archives, and the **transcript is written
   to the archive**.

Every view that replaces the card re-adds the **Chat history** button. That is not tidiness: an
edit which omits `attachments` leaves them alone, so the chat log used to survive all of step 6
and 7 for free as a file — components get no such treatment, and without re-adding it a decided
ticket would lose its chat at exactly the moment a reviewer goes back to check something.

**Every state two people can trigger at once is conditional at the database, not a
read-then-write.** Approve and Claim both return whether the caller won, and the loser is told
who beat them. That is not defensive coding: without it, two reviewers approving simultaneously
recorded two kits and burned the applicant's 21-day cooldown twice for one request.

Nothing about dispatch is automated. The queue is a claim board, not a work assigner.

**A ticket can always be got rid of.** `/close` handles the undecided ones, `on_thread_delete`
plus a re-check on every button press handles a deleted thread, and a failed queue post
auto-closes. All three exist because the panel's pre-check counts open tickets, so anything
that leaves one open forever silently bars that person from ever asking again.

---

## What the card says, and what it refuses to say

Ordered by what settles a decision fastest — see
[../docs/reviewing.md](../docs/reviewing.md) for the reasoning:

1. **Recent deaths.** A death from eleven minutes ago with its message and killer attached
   *is* the "I lost everything" claim, already verified. Most approvals need nothing else.
2. **Time on 2b2t.** First seen, last seen, playtime, counts. First-seen substitutes for a
   Minecraft account creation date, which is genuinely unobtainable for anyone but the
   account's owner — and is the better number here anyway, since it cannot be bought.
3. **Kit ledger.** Cooldown checked against the Discord id **and** the UUID independently,
   because either alone is trivially sidestepped.
4. **Reviewer flags.**
5. **Recent chat** — lexicon counts on the card, and a **Chat history** button that pages the
   lines themselves in an ephemeral panel rather than making you download a file to read them.

A **project funding** card leads with **The ask** instead — what the project is, what they need
and roughly how big it is. The two kinds are judged on close to opposite evidence: a death an
hour ago is the entire case for a rescue kit and says nothing about whether to fund a build, so
a funding headline reports time on the server and past grants, and the rescue-only "claim
verified" line is suppressed.

Above all of it sits a one-line call — **Nothing against them** / **Worth a closer look** /
**Blocked** — and every rule that fired with the evidence it read. Chat for that is swept over
the last 365 days, page-walked to a cap; the trace states whether it read the whole year or only
a sample, because "nothing found" in 12 lines and in 12 of 2,595 are different claims.

Three things it will not do:

- **No score, anywhere.** Not on chat, not on the account. A number gets read as the answer
  to a question counts cannot answer. There is a test asserting no card field is named
  `score`, `confidence`, `risk` or `verdict`, and the call above is a **named rule trace**
  rather than a number for exactly that reason: it names the claim so a reviewer can disagree
  with that claim. Two limits keep it honest, both measured rather than asserted — profanity and
  slur counts never move it (the tuned lexicon flags 3.7% of *all* 2b2t chat, so it would move
  against the median player), and chat can never produce a **Blocked**, which only a
  do-not-serve flag or the cooldown can.
- **No "probable alt" badge.** Low playtime on an old account describes an alt *and* a
  returning lapsed player identically. The card says exactly that, in those words, instead of
  leaning. "Known alt" is a reviewer-maintained flag, never a computation.
- **A missing section never renders as an empty one.** "Could not retrieve deaths" and "no
  deaths" mean opposite things, and collapsing the first into the second would decline people
  for having a clean record. Failures say so and land in an `Incomplete` field.

---

## Privacy

**Chat reaches Discord only through the reviewer-only pager, already redacted.** Coordinate
redaction happens inside `card.gather`, at the boundary, so no display path can be the one that
forgot — and `screening.redact_coords` cannot be set false while screening is on, because a
config file is not a good place to be able to turn a privacy control off.

This applies to **chat scraped from api.2b2t.vc**, not to what an applicant types into a form
about themselves. A meeting point exists to be read by whoever is flying out to it, and a
project description that names a location is how a build gets described; redacting either would
break the feature rather than protect anyone. Neither is ever written to a log record — the
journal says a meeting point was set, never what it was.

The reason is that the coordinates in someone's chat are usually **somebody else's**: a base
leaked in an argument, a stash traded in public, a location someone is being hunted to. The
applicant consented to review; the third party in their log did not, and a Discord thread is
durable, searchable and screenshot-able.

Redaction is heuristic and biased toward over-redacting. A reviewer reading `[coords]` where
two large numbers used to be loses nothing that matters to the decision; a leaked base costs
someone their base. The card reports how many values were hidden rather than pretending the
line always read that way.

Nothing here logs chat. Log records carry ticket ids, user ids, counts and status codes.

---

## Instrumentation

`/flagline` and the `shown_chats` table exist because **production is the only source of
labelled screening data, and it cannot be backfilled.** Which lines a reviewer saw, and which
ones they objected to, is a judgement that exists at the moment it is made and nowhere else.

Every ticket stores the lines that were shown (redacted). Every `/flagline` adds a label.
Every decision stores its reason. After a few months that is a real evaluation set — the thing
that turns "is our lexicon any good" from an opinion into a measurement. Without it you are
still guessing, and no amount of later effort recovers it.

---

## Rate limits

`api.2b2t.vc` allows **5 requests per second across every caller on the internet**, not per
IP, and its 429s carry no `Retry-After`. So:

- The pacer is **module-level, shared by every client in the process** — per-instance pacing
  would let two concurrent reviews double the rate.
- `vc.min_interval_s` will not go below 0.2; the config refuses it.
- A 429 backs off and retries. If it gives up, the card renders with that section marked
  failed rather than blank.
- `/stats/player` is one request for first-seen, last-seen, playtime and all four counts,
  which is why the card is three calls and not seven.

A card costs about four requests and a few seconds. There is no job queue and does not need
to be one.
