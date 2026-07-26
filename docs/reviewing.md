# Reviewing a request

## The question you are answering

**Not** "is this person good enough for a kit."

**Is** "are they actually new, or did they actually get wiped — and have we already given them
three."

The kits are barebones diamond kits. They are meant to be lost. Declining someone who
genuinely needed one costs the group more than handing one to someone who didn't, so when the
card is ambiguous, the tie goes to the applicant.

---

## The line at the top of the card

Every card opens with **Nothing against them**, **Worth a closer look**, or **Blocked**, and
under it every rule that fired with the evidence it read. It is a table of contents, not a
substitute for the card: each line tells you where to go and check.

It is deliberately **not a score.** A score is one number standing in for a judgement nobody can
audit; a trace names the claim so you can disagree with that specific claim in one glance.

Two limits on it, both measured rather than assumed:

- **Profanity and slur counts never move it.** Run the tuned lexicon over a year of real 2b2t
  chat and it still matches 3.7% of *everything*. Profanity is the server's register, so a
  recommendation that moved on it would be recommending against the median player. The counts
  stay on the card; they stay out of the arithmetic.
- **Chat can never say Blocked.** Only two rules can, and both are facts rather than inferences:
  a reviewer's own do-not-serve flag, and the cooldown. The strongest thing chat does is *read
  these lines first* — because whether a matched phrase is a real threat or somebody asking for
  a delivery address is exactly what a keyword list cannot see. The kits are disposable, so a
  false deny costs more than a false approve, and the tie goes to the applicant.

**Blocked is a recommendation, not a decision.** Approve over it if you disagree; the reason you
type is what the ledger keeps.

The one chat category that *does* move it is `off_game` — statements that leave the game, meaning
they target a person rather than a player. That is the category's whole purpose, and when it
fires the trace names which line numbers to read.

### How much chat it actually read

The trace ends with what it looked at, and this matters more than it sounds. Chat is swept over
the **last 365 days**, page-walked up to a cap. Most of the time it reads the whole year — the
median 2b2t player says **twelve lines in a year**, and the cap covers ~94% of players
completely. For the talkative minority it stops early and says so:

> read the most recent 500 line(s) of 2595 in the last 365 days - the rest was not examined

Treat that exactly like "could not retrieve deaths". Nothing found in a sample is not the same
claim as nothing found in a year, and the card will not pretend otherwise.

## Two kinds of request, judged on different evidence

A **rescue kit** and **project funding** arrive in the same queue and are close to opposite
decisions, so the card reorders itself depending on which it is.

For a rescue kit, a recent death is the claim already verified and most of the decision. For
funding it is irrelevant — it verifies a claim nobody made — so a funding card leads with **The
ask** (what the project is, what they need, how big it is), and its headline reports time on the
server and past grants rather than the death. The rescue-only "this is the lost-everything
claim, verified" line is suppressed on a funding card for the same reason.

The question above also changes shape. For funding it is not "did they get wiped" but "is this a
real project and have we already funded it" — and there is no equivalent of the death line to
settle it, so expect to read the whole card.

The two have **separate cooldowns**. Someone funded for a build three days ago is not inside a
cooldown for a rescue kit, and vice versa; a card reports whichever one applies to the request in
front of you. `/lookup`, which has no request behind it, reports *any* grant.

---

## The card, and what each line is actually worth

### Recent deaths — read this first

A death from eleven minutes ago, with the death message and the killer's name attached, **is
the "I lost everything" claim, already verified.** Nothing else on the card comes close, and
most approvals can be made on this line alone.

What it cannot tell you is whether the *inventory* was worth anything. It doesn't need to —
the kit is disposable.

### First seen on 2b2t

This is the substitute for a Minecraft account creation date, which is
[genuinely unobtainable](external-apis.md#account-creation-date-is-not-obtainable-at-all) for
anyone but the account's owner. It is also the better number for this decision, because
account age can be bought and 2b2t history cannot.

A first-seen of yesterday supports "just getting started" directly.

### Playtime against first seen

Low playtime with an **old** first-seen is the ambiguous case, and it is worth being explicit
about why: it describes an alt **and** a returning lapsed player equally well. There is no
signal here that separates them. Do not let a low number decide anything by itself.

### Recent public chat

A list for you to read. Keyword counts sit beside it as a hint, never as a verdict.

**Profanity is 2b2t's ambient register**, so treating it as disqualifying disqualifies
practically everyone, including the people handing out the kits. And "I'll kill you" is a
description of gameplay.

The only distinction worth drawing is whether a statement **leaves the game** — targeting
someone as a person rather than as a player, slurs, threats that aren't about Minecraft. That
line is a social judgement, not a technical one, which is exactly why a human reads the list
and the tooling only counts.

2b2t has since added its own chat filter, so expect this section to get quieter and less
informative over time.

### Kit ledger

Has this person, or an account tied to them, had a kit inside the **21-day cooldown**.

This is the real anti-abuse surface. **The abuse case is repeat farming, not toxicity** — and
the ledger plus the cooldown is the whole defence, which is why it is the one part worth
keeping accurate.

### Known-alt flag

A **reviewer-maintained list**, not a detection. One click to set, and it outlives the
reviewer who recognised the name — which is the entire point, since that recognition is
otherwise lost the moment they stop reviewing.

The card will never show a confident "probable alt" badge, because
[nothing can compute one](#playtime-against-first-seen).

---

## Declining

The reason field is required. Write it for someone reading the ledger a year from now with no
memory of this ticket — "known alt of \<name\>" is useful, "no" is not.

**The applicant sees only the outcome.** Not the card, not the flag list, not your reason — that
text is for whoever reads the ledger later, and it names criteria and sometimes other accounts.
If you want to say something to them, type it in their thread.

## Not deciding

A ticket you cannot decide should be **closed, not left**: `/close <ticket> <reason>`. The
panel's pre-check counts open tickets, so one left open forever quietly stops that person from
ever asking again. "No reply in a week" is a perfectly good reason, and closing records no kit,
so it never burns anyone's cooldown.

If a delivery stalls — somebody claimed it and went quiet — `/unclaim <kit>` puts it back in the
pool without touching the approval. The kit is still owed; it just needs another runner.

## Where finished tickets go

Everything that closes writes a transcript to the staff archive: the decision and its reason,
the chat that was shown, and the applicant conversation. It is assembled from the ledger, so it
survives the thread being deleted — you can clean threads up freely without losing the record.

The one thing that **cannot** be reconstructed later is which chat lines you objected to. That
is what `/flagline <ticket> <lines>` captures, and it is the only labelled screening data this
project will ever get. See [chat-screening.md](chat-screening.md).

## Reading the chat

The card gives you counts; **Chat history** on the queue post gives you the lines. It opens a
panel only you can see, twenty lines a page, with Previous / Next and a **Download .txt** if you
would rather search the whole thing at once. Flagged lines carry a `!` in the left column.

The numbers down the left are the ones `/flagline` wants — they are positions in the stored log,
not positions on the page, so they stay correct however you got there.

Two things worth knowing. The panel is ephemeral, so it disappears when your Discord client
restarts; press the button again, it is always there on the post, including after the ticket is
decided and archived. And if a ticket has **no** chat recorded, the button is simply absent —
because an empty log cannot tell you whether the player said nothing or the lookup failed, and
the card's *Recent public chat* field is the thing that distinguishes those.
