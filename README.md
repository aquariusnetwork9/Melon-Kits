# Melon-Kits

Tooling for the **Melon Men** kit programme on 2b2t: a Discord app that takes kit requests,
puts a short factual card in front of a reviewer, and dispatches an approved kit to whoever
claims the delivery.

The kits are barebones diamond kits — deliberately disposable — handed to players just
getting started and to anyone who has just lost everything. That single fact sets the whole
design. **Declining someone who deserved a kit costs more than handing a kit to someone who
didn't**, so the review step is built to answer *"are they actually new, or did they
actually get wiped, and have we already given them three"* — not *"is this person good
enough."* Anything that made the review slower, heavier or more judgemental than that got
cut.

---

## Running it on your own server

On any Linux box with systemd:

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Melon-Kits/main/install.sh | sudo bash
```

It asks for your bot token and nothing else, sets everything up under a dedicated unprivileged
user, and prints the invite link plus the one remaining step. Then run **`/setup`** in your
server as somebody with Manage Server — it offers to adopt the roles and channels you already
have, and creates whatever is missing.

Re-run the same command to upgrade; it never overwrites your token, config or ledger.
`--uninstall` removes the service and keeps the data. Details, and every trap worth knowing
before changing any of it, are in [kit-app/deploy/DEPLOY.md](kit-app/deploy/DEPLOY.md).

You will need a bot application at
[discord.com/developers/applications](https://discord.com/developers/applications) —
**Bot → Reset Token** gives you the token to paste in. No privileged intents are required.

---

## Status

| component | state |
|---|---|
| [`kit-app/`](kit-app/) — Discord ticket → review → dispatch | **v1.0.0, live and exercised end to end.** 132 tests. Multi-server: invite it anywhere and run `/setup`. Deployed on ovh-2; a full request → review → approve → claim → deliver → archive lifecycle has been run through real Discord component presses |
| [`chat-corpus/`](chat-corpus/) — passive 2b2t chat collector | **built and tested, not yet running unattended.** 249 tests, green on Windows/3.9 and Linux/3.13, verified against the live feed, both SPEC §13 crash-safety tests in place |
| Chat screening lexicon | **scaffolded, empty.** The mechanism works and is tested; the terms need an afternoon of mining against the 2025 bulk dump |

**No real applicant has used it yet** — every ticket so far has been a test on a test server.
Two things in the panel copy are stated intent rather than enforced behaviour: the 24–48 hour
response time is not tracked or alerted on, and "die with your kit and you get no other" needs
a reviewer to set a `deny` flag, since the automatic cooldown is 21 days for everyone
regardless of how the last kit ended.

---

## Layout

```
docs/                 project documentation (see below)
kit-app/              the Discord app: panel, ticket, reviewer card, dispatch, archive
  bot.py              the ONLY file that imports discord.py
  card.py             reviewer card assembly — Discord-free, so it is testable
  redact.py           coordinate redaction, applied at the display boundary
  screening.py        normalisation + keyword counts (no score, by design)
  store.py            SQLite ledger: cooldowns, flags, decisions, instrumentation
  vc.py identity.py   api.2b2t.vc client; Mojang + laby.net name resolution
  deploy/             systemd unit, channel setup, ticket reset, smoke test, runbook
  tests/              132 tests, no network, and no discord.py needed
chat-corpus/          the collector: SSE feed → append-only JSONL, stdlib only, crash-safe
  docs/SPEC.md        full behavioural spec
  docs/INTERFACES.md  binding module API
  tools/              export / dedupe-audit / gap-injection / stats
  tests/              249 tests, no network, no third-party deps
```

| document | what it covers |
|---|---|
| **[docs/operations.md](docs/operations.md)** | **running it: everyday tasks, symptom→fix table, and every trap that cost real time.** Start here if something is broken |
| [docs/kit-request-flow.md](docs/kit-request-flow.md) | panel → ticket → decision → dispatch → archive, and what the design deliberately omits |
| [docs/reviewing.md](docs/reviewing.md) | the reviewer card line by line, and what each signal is actually worth |
| [docs/chat-screening.md](docs/chat-screening.md) | the keyword list, why it isn't a model, and the DuckDB queries that make it good |
| [docs/external-apis.md](docs/external-apis.md) | verified findings on `api.2b2t.vc`, Minecraft identity, and Discord — the counter-intuitive ones only |
| [kit-app/README.md](kit-app/README.md) | the bot itself: install, Discord setup, the eight commands, the three channels |
| [kit-app/deploy/DEPLOY.md](kit-app/deploy/DEPLOY.md) | how the ovh-2 deployment is built, and how to rebuild it |
| [chat-corpus/README.md](chat-corpus/README.md) | running the collector: quickstart, runbook, health states, disk |

The collector and the app share no code and no runtime dependency in either direction, so
either can be run, moved or replaced without touching the other. The app needs one package
(`discord.py`) and confines it to `bot.py`; the collector needs none at all, ever.

---

## How the pieces fit

**The review card is four requests and a few seconds.** No job queue, **no privileged Discord
intents at all** — not even for transcript conversation capture, which reads history over REST
where `MESSAGE_CONTENT` does not apply — and no model. It was scoped as five calls against
`api.2b2t.vc`; building it made one redundant, because `/stats/player` turns out to return
first-seen, last-seen, playtime *and* all four counts in a single request, which matters
against a rate limit shared with the whole internet:

- Mojang — name to UUID, so the ledger and any flag survive a rename.
- `/stats/player` — first seen, last seen, playtime, counts. **First-seen is the substitute
  for a Minecraft account creation date**, which is genuinely unobtainable for arbitrary
  accounts and always has been; it is also the more meaningful number here, and unlike an
  account age it cannot be bought.
- `/deaths` — the highest-value call in the set. A death from eleven minutes ago, with the
  death message and the killer's name attached, **is the "I lost everything" claim, already
  verified**. Most approvals can be made on that line alone.
- `/chats` — the applicant's recent public chat, listed for a human to read. Keyword counts
  sit beside it as a hint, never as a verdict.
- The kit ledger — has this person, or an account tied to them, been given a kit inside the
  cooldown.

**Chat screening is intentionally cheap.** Profanity is 2b2t's ambient register, so flagging
it flags everybody, and "I'll kill you" is a description of gameplay. The only distinction
worth drawing is whether a statement *leaves the game*. 2b2t has since added its own chat
filter, so this is a feature with a declining ceiling — build it cheap, don't invest further.

**The real abuse surface is repeat farming, not toxicity**, which makes the ledger plus a
21-day cooldown the whole anti-abuse story.

**"Known alt" is a list, not an algorithm.** Nothing detects alt-ness: low playtime against
an old first-seen describes an alt *and* a returning lapsed player equally well. It is a
reviewer-maintained flag — one click, and it outlives the reviewer who recognised the name.
The app will never show a confident "probable alt" badge.

---

## Why the chat corpus exists

Screening needs a lexicon, and a lexicon written from imagination is a lexicon of spellings
nobody uses. The corpus is the ground truth for that: normalise it, then look back at the
original spellings ranked by frequency, and you get the evasion spellings people **actually**
type instead of the leetspeak rules you'd have guessed.

That is *mining*, not training. The 2025 bulk dump and the live corpus are both **unlabelled**,
so supervised training would mean days of hand-labelling first for a result a keyword list
already delivers. Labels do arrive free from production — which chat lines were shown, which
the reviewer flagged, and every decision with its reason — but only if that is instrumented
from the first day. It cannot be backfilled.

The collector runs continuously because 2b2t's own chat filter changes what gets said. It
makes the corpus *less* informative about content and plausibly *more* informative about
orthography, which is the part that matters here.

---

## Privacy

2b2t public chat routinely carries **other people's** base coordinates — a base leaked in an
argument, a stash traded in public, a location someone is being hunted to. The applicant
consented to their chat being reviewed; the third party in their log did not. Both halves of
this repository are built around that.

**Nothing anywhere logs chat.** Log records carry ids, counts, status codes, byte offsets and
file basenames. An exception message that would interpolate a record body is treated as a
defect, so exception text is assembled from metadata instead. Every example in this repository
— READMEs, docstrings, tests — uses synthetic text, names and UUIDs, and any coordinate that
appears is fabricated inside 20k of the origin.

**In the collector**, chat lives in exactly one place: the corpus JSONL files. One tool
(`export_csv.py`) is allowed to emit chat text, because writing it to a file you asked for is
its entire job — and it refuses to write to a terminal.

**In the app**, coordinate redaction happens inside `card.gather`, at the boundary, so no
display path can be the one that forgot; the config refuses to let it be switched off while
screening is on; and the ledger stores the already-redacted text, so the database is not a
back door round the display rule. Redaction is biased toward over-redacting: a reviewer
reading `[coords]` where two large numbers were loses nothing that changes a decision, and a
leaked base costs someone their base.

**Applicants are told.** The request panel states that public 2b2t history — first seen, recent
deaths, public chat — is checked, and that nothing private is looked at. Applicants never see a
reviewer card, and a decline gives them the outcome without the internal reason, which is
written for whoever reads the ledger in a year rather than for its subject.

The applicant-facing request panel carries the vetting disclosure directly — that help is
voluntary and best-effort, that a kit is never guaranteed, and the three reasons a request
gets declined. That copy doubles as the privacy notice, so there is no separate one to go
unread.
