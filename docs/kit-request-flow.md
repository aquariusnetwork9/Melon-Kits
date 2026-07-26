# Kit request flow

From a player wanting a kit to a team member handing one over.

---

## 1. The front door

A **pinned panel** in a read-only `#kit-requests` channel with a single **Request a kit**
button. The button carries a static `custom_id`, so it keeps working across bot restarts and
redeploys forever — nothing about the panel is stateful.

The panel copy carries the volunteer / best-effort / not-guaranteed warning and names the
three reasons a request gets declined. **That copy doubles as the vetting disclosure**, so
there is no separate privacy notice for anyone to skip.

Draft:

> ### 🍈 Melon Men — Kit Requests
>
> We hand out barebones diamond kits to players just getting started on 2b2t, and to anyone
> who's just lost everything.
>
> - A kit is never guaranteed.
> - We can't promise a waiting time.
> - We check a few things first. Requests usually get declined for one of these:
>   - the account looks like an alt of someone we've already helped
>   - public chat history we aren't willing to overlook
>   - you've had a kit from us in the last 21 days

**Pre-check before the modal, not after.** On button click, check for an existing open ticket
and for the cooldown *first*, and reply ephemerally if either blocks. Making someone fill in a
form and then telling them they were never eligible is the one avoidable bad experience in
this flow.

## 2. The ticket

Modal collects the Minecraft username and a free-text note. On submit, a **private thread**
hangs off the panel channel with the applicant and the reviewer role in it.

The bot posts the reviewer card into the thread. See [reviewing.md](reviewing.md) for what is
on it and how to read it.

## 3. The decision

Two buttons for the reviewer role: **Approve** and **Decline**. Decline opens a modal for the
reason, which is required — the reason is what makes the ledger useful a year later, and an
optional field is an empty field.

Both outcomes post into the thread and update the ledger.

## 4. Dispatch

An approval places a **dispatch ember** in the team channel: the username, the approval, and a
**Claim** button. Whoever presses it — person or bot — owns the delivery, and the ember shows
who claimed it.

Nothing about dispatch is automated. The ember is a claim board, not a queue.

---

## What this deliberately does not have

- **No job queue.** Five API calls, ~5 seconds, run inline behind a deferred interaction.
- **No gateway connection** beyond what interactions need.
- **No model, no inference, no training.**
- **No identity binding by default.** An in-game whisper code to prove the Discord user
  controls the Minecraft account is *available* using existing proxy tooling, but the cooldown
  only needs a stable identifier and the Discord user ID already is one. It was downgraded
  from a phase-0 requirement to an option because it costs the applicant real effort to defeat
  an abuse case that has not happened yet.

---

## Instrument this from day one

Log every decision with its reason, which chat lines were shown to the reviewer, and which
lines the reviewer flagged.

That is the only labelled data this project will ever get for free, and **it cannot be
backfilled** — a decision's reasoning exists at the moment it is made and nowhere else. Six
months of it turns chat screening from guesswork into something measurable. Six months
without it leaves you exactly where you started.
