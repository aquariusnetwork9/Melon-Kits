# Reviewing a request

## The question you are answering

**Not** "is this person good enough for a kit."

**Is** "are they actually new, or did they actually get wiped — and have we already given them
three."

The kits are barebones diamond kits. They are meant to be lost. Declining someone who
genuinely needed one costs the group more than handing one to someone who didn't, so when the
card is ambiguous, the tie goes to the applicant.

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
