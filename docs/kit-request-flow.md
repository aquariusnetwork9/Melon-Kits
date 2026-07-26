# Kit request flow

From a player wanting a kit to a team member handing one over.

---

## 1. The front door

A **pinned panel** in a read-only `#kit-requests` channel with two buttons — **Request a rescue
kit** and **Request project funding**. Both carry static `custom_id`s, so they keep working
across bot restarts and redeploys forever; nothing about the panel is stateful. The rescue
button kept the original id it shipped with, which is what lets an already-pinned panel be
*edited* to gain the second button instead of deleted and re-posted.

**Two buttons rather than one form with a type selector**, and that is Discord's constraint, not
a preference: a modal is submitted whole, so it cannot change its own questions after somebody
picks a type inside it. Funding needs to ask what the project is, what materials are needed and
roughly how big it is; a rescue request needs none of those and a single shared "anything we
should know?" box would make both answers unusable.

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

A rescue modal collects the Minecraft username and a free-text note; the funding modal collects
the username, what the project is, what they need and roughly how big it is. On submit, a
**private thread** hangs off the panel channel with the applicant and the reviewer role in it.

The type travels with the ticket and shows up in four places — the queue post's title, the top
of the card, the forum tag and the transcript. The **tag is a convenience only**, and
deliberately not the mechanism: a server that ran `/setup` before those tags existed cannot
re-run it without being granted Manage Channels back, which `/setup` itself tells admins to
remove. So the type is in the title and the card, where it cannot silently fail to appear.

The bot posts a **receipt** in that thread — and nothing else. The reviewer card goes to a
separate **staff-only forum**, one post per ticket, tagged `awaiting review`. See
[reviewing.md](reviewing.md) for what is on the card.

A **rescue** receipt carries a reading list with it: both escape guides, the client setup guide
and the guides category. Funding tickets do not get it — those guides are about getting out of
spawn alive, which is not what somebody asking for build materials came for. The channel ids are
hardcoded in `bot.py` (`HELP_GUIDES`), because this bot is deployed to one server and four more
`/setup` questions would be four more ways to misconfigure a constant.

They are sent as `<#id>` mentions, which matters more than it looks: Discord resolves a channel
mention against **the reader's** permissions, not the bot's. The links therefore work whether or
not the bot can see those channels, and the bot does not need to be in the server for the code
to be correct. The embed is deliberately *not* gated on `guild.get_channel()` — that would have
hidden the reading list over a bot permission with no bearing on whether the applicant can click
it. The trade is that in any other server these render as `#unknown-channel`.

**The applicant never sees the card.** It carries the ledger fan-out, the reviewer flag list
and the screening counts. Showing someone the exact criteria applied to them exposes notes
written about them and teaches them how to clear the bar next time — and the fan-out can name
other accounts. That split is the whole reason there are two channels.

**The panel channel has to be a text channel.** Forum posts are always public threads
(`ForumChannel.create_thread` has no privacy parameter) and threads carry no permission
overwrites of their own, so visibility follows the parent channel. Putting tickets in a forum
would let every applicant read every other applicant's card. A Discord constraint, not a
preference.

## 3. The decision

Two buttons for the reviewer role on the queue post: **Approve** and **Decline**. Decline opens
a modal for the reason, which is required — the reason is what makes the ledger useful a year
later, and an optional field is an empty field.

**The applicant is told the outcome, never the internal reason.** "Known alt of \<name\>" is
written for whoever reads the ledger in a year, not for the person it describes. A decline
points back at the panel, which already lists the three usual reasons, and says the decision
isn't permanent. A reviewer who wants to say more can type it in the thread.

## 4. Dispatch

An approval **turns the same queue post into the dispatch**: retagged `approved`, a **Claim**
button attached, the card left in place. Whoever presses Claim owns the delivery and is
introduced in the applicant's thread. Marking delivered retags and archives the post —
archived rather than locked, so a delivery that falls through afterwards can be reopened.

**Approval is also when the applicant is asked where to meet.** Their thread gets a **Set
meeting coordinates** button, and what they enter appears on the queue post and in the claim
confirmation, so whoever picks the job up already knows where they are going. It is asked at
approval rather than on the request form for two reasons: before a decision there is nothing to
deliver, and asking up front would collect a location from every person who gets declined. They
can press it again if they move, and anything written alongside the three numbers is kept — "by
the big cobble tower" is useful to whoever is flying out.

One post is therefore the entire record of a ticket — request, card, decision, claim, delivery
— instead of a card in one channel and an ember in another, and tags make the queue filterable:
`awaiting review`, `approved`, `declined`, `claimed`, `delivered`.

Nothing about dispatch is automated. The queue is a claim board, not a work assigner.

## 5. The archive

Whenever a ticket finishes — delivered, declined, closed, or auto-closed because its thread was
deleted — one transcript is posted to a **staff-only archive channel**: a compact embed plus an
attachment with the full decision history, every chat line shown to the reviewer, and the
applicant ↔ staff conversation.

It is assembled **from the ledger, not by reading Discord back**, so it survives the thread and
the queue post being deleted. That is the point: a ticket's record should outlive the places it
happened in.

## 6. Getting rid of a ticket

The panel's pre-check counts *open* tickets, so **anything that leaves a ticket open forever
silently bars that person from ever requesting again.** Given the kits are disposable and
under-helping is the expensive direction, that is the worst failure this design can have, so
there are three separate ways it cannot happen:

- **`/close <ticket> <reason>`** for a ticket nobody will decide — the applicant went quiet,
  withdrew, or it was a duplicate. Open to the **applicant** as well as to reviewers: closing
  your own request only frees your own slot, and needing to find staff for that is friction with
  no upside. It records no kit, so it never burns the cooldown.
- **A deleted thread** closes the ticket automatically, both on the delete event and by
  re-checking on every button press, since a deletion while the bot is offline fires no event.
- **A queue post that fails to send** auto-closes, because no reviewer would ever see it.

**`/unclaim <kit>`** is the delivery-side equivalent: a runner handing back a job, or a reviewer
prising a stale claim off someone who has gone quiet. The ticket stays approved — the kit is
still owed, it just needs somebody else.

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
