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

## Status

| component | state |
|---|---|
| [`chat-corpus/`](chat-corpus/) — passive 2b2t chat collector | **built, tested, verified against the live feed.** Not yet running unattended (see its README for the one outstanding test) |
| Discord app — ticket → review → dispatch | **scoped, not built** |
| Chat screening lexicon | **not started.** Blocked on nothing; wants an afternoon against the 2025 bulk dump |

Nothing here has served a real kit request yet.

---

## Layout

```
chat-corpus/          the collector: SSE feed → append-only JSONL, stdlib only, crash-safe
  docs/SPEC.md        full behavioural spec
  docs/INTERFACES.md  binding module API
  tools/              export / dedupe-audit / gap-injection / stats
  tests/              244 tests, no network, no third-party deps
```

The Discord app will land alongside it. The collector is deliberately standalone — it has no
dependency on the app and the app has no runtime dependency on it, so either can be run,
moved or replaced without touching the other.

---

## How the pieces fit

**The review card is five API calls and about five seconds**, all against the public
`api.2b2t.vc`. No job queue, no gateway connection, no privileged intents beyond Discord
message-content, and no model:

- `/seen` and `/stats/player` — first seen on 2b2t, last seen, playtime. **First-seen is the
  substitute for a Minecraft account creation date**, which is genuinely unobtainable for
  arbitrary accounts and always has been; it is also the more meaningful number here, and
  unlike an account age it cannot be bought.
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

2b2t public chat routinely carries other people's base coordinates. Per a standing operator
rule, **chat content lives in exactly one place — the corpus JSONL files.** It never reaches
a log, a status file, an alert, a traceback or a terminal, and every example in this
repository uses synthetic text, names and UUIDs. The collector's README documents how that is
enforced and which single tool is allowed to emit chat text at all.

The applicant-facing request panel carries the vetting disclosure directly — that help is
voluntary and best-effort, that a kit is never guaranteed, and the three reasons a request
gets declined. That copy doubles as the privacy notice, so there is no separate one to go
unread.
