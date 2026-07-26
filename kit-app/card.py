"""Builds the reviewer card. Deliberately knows nothing about Discord.

The card answers one question -- **"are they actually new, or did they actually get wiped, and
have we already given them three"** -- and is ordered so the line that usually settles it
comes first. See docs/reviewing.md for why that is the question and not "is this person good
enough".

Two rules this module enforces rather than leaves to its caller:

- **Every chat line is coordinate-redacted here**, before it is returned, so no display path
  can accidentally be the one that forgot.
- **No signal is presented as a conclusion it cannot support.** Low playtime against an old
  first-seen describes an alt *and* a returning lapsed player, so it renders as an explicit
  "cannot distinguish" rather than a lean either way. There is no confidence score anywhere
  in this file, and adding one would be a regression.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional

import redact
import screening
import store as store_mod
import vc as vc_mod

_LOG = logging.getLogger("melonkit.card")


# --------------------------------------------------------------------------- time

def parse_ts(text: Optional[str]) -> Optional[datetime.datetime]:
    """Parse the API's ISO-8601 timestamps, with or without fractional seconds.

    ``datetime.fromisoformat`` cannot read a trailing 'Z' on Python 3.9 and the API emits
    both ``...:24.084Z`` and ``...:47Z``, so this normalises before handing it over.
    """
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def parse_ts_epoch(seconds: Optional[int]) -> Optional[datetime.datetime]:
    """Unix seconds (how the ledger stores time) to an aware UTC datetime."""
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(int(seconds), datetime.timezone.utc)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def at_epoch(secs: Optional[int]) -> Optional[datetime.datetime]:
    """Ledger timestamps are epoch ints; `ago` wants an aware datetime. Bridge, so the
    conversion is written once instead of at every call site that mixes the two."""
    if not secs:
        return None
    return datetime.datetime.fromtimestamp(int(secs), datetime.timezone.utc)


def ago(when: Optional[datetime.datetime], now: Optional[datetime.datetime] = None) -> str:
    """Human relative time. 'never' when unknown -- never a blank."""
    if when is None:
        return "unknown"
    now = now or _now()
    secs = (now - when).total_seconds()
    if secs < 0:
        return "in the future"
    if secs < 90:
        return "%d seconds ago" % int(secs)
    mins = secs / 60.0
    if mins < 90:
        return "%d minutes ago" % int(round(mins))
    hours = mins / 60.0
    if hours < 36:
        return "%d hours ago" % int(round(hours))
    days = hours / 24.0
    if days < 60:
        return "%d days ago" % int(round(days))
    if days < 730:
        return "%d months ago" % int(round(days / 30.44))
    return "%.1f years ago" % (days / 365.25)


def duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "none recorded"
    secs = int(seconds)
    if secs < 60:
        # Integer-dividing a sub-minute span rendered as "0 minutes", which showed up in the
        # archive on any ticket handled quickly -- exactly the ones worth noticing.
        return "%d seconds" % secs
    if secs < 3600:
        return "%d minutes" % (secs // 60)
    if secs < 86400 * 2:
        return "%.1f hours" % (secs / 3600.0)
    return "%.1f days (%d hours)" % (secs / 86400.0, secs // 3600)


# --------------------------------------------------------------------- gathering

def gather(guild_id: int, mc_name: str, mc_uuid: Optional[str], discord_user_id: int,
           cfg: Dict[str, Any], client: "vc_mod.Client", st: "store_mod.Store",
           lex: Optional[screening.Lexicon] = None,
           log: Optional[logging.Logger] = None,
           *, request_type: Optional[str] = None,
           details: Optional[Dict[str, Any]] = None,
           ticket_id: Optional[int] = None) -> Dict[str, Any]:
    """Everything the card needs. Partial failures degrade rather than abort.

    A review must not be impossible because one endpoint is briefly rate-limited: whatever
    was retrieved still goes in front of the reviewer, and whatever failed says so out loud
    rather than rendering as an innocent-looking zero. A missing death list and an empty
    death list mean opposite things.

    `request_type` and `details` are keyword-only, and deliberately appended rather than
    inserted: every existing caller passes the first eight arguments positionally, so a
    parameter added anywhere earlier would silently become the lexicon.

    `request_type=None` means "not about a particular request" -- what `/lookup` does when a
    reviewer vets a name with no ticket in front of them. It matters because it decides which
    cooldown is reported: scoped to a kind for a real ticket, and *any* grant for a lookup.
    Defaulting it to rescue instead would make `/lookup` answer "cooldown clear" for someone
    whose only grant was funding three days ago, which is the exact question the lookup exists
    to answer.
    """
    log = log or _LOG
    pol = cfg["policy"]
    card: Dict[str, Any] = {
        "guild_id": int(guild_id),
        "mc_name": mc_name,
        "mc_uuid": mc_uuid,
        "discord_user_id": int(discord_user_id),
        "request_type": request_type,
        "details": details or {},
        "errors": [],
        "generated_at": _now(),
    }

    def attempt(key: str, fn):
        try:
            return fn()
        except (vc_mod.VcError, vc_mod.VcUnavailable) as exc:
            card["errors"].append("%s: %s" % (key, exc))
            log.warning("card section failed section=%s err=%s", key, type(exc).__name__)
            return None

    stats = attempt("stats", lambda: client.stats(uuid=mc_uuid, name=mc_name))
    card["stats"] = stats
    card["tracked"] = stats is not None

    deaths_doc = attempt("deaths", lambda: client.deaths(
        uuid=mc_uuid, name=mc_name, limit=pol["recent_deaths"]))
    card["deaths"] = (deaths_doc or {}).get("deaths") or []
    card["deaths_total"] = (deaths_doc or {}).get("total")
    card["deaths_ok"] = deaths_doc is not None

    # A dated window when one is configured, otherwise the old "most recent N lines". The
    # window is what makes a clean result mean something: without it, 100 lines is three years
    # for a quiet player and four days for a talkative one, so "nothing in their chat" was
    # silently a different claim for every applicant.
    window_days = int(pol.get("chat_window_days") or 0)
    if window_days > 0:
        start = (_now() - datetime.timedelta(days=window_days)).strftime("%Y-%m-%d")
        end = (_now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        chats_doc = attempt("chats", lambda: client.chats_window(
            uuid=mc_uuid, name=mc_name, start=start, end=end,
            max_pages=int(pol.get("chat_max_pages") or 5)))
    else:
        chats_doc = attempt("chats", lambda: client.chats(
            uuid=mc_uuid, name=mc_name, limit=pol["recent_chats"]))
    raw_rows = (chats_doc or {}).get("chats") or []
    card["chats_total"] = (chats_doc or {}).get("total")
    card["chats_ok"] = chats_doc is not None
    card["chat_window_days"] = window_days
    # True only when every line in the window was read. `chats` has no cap to hit, so it is
    # complete by definition of what it claims to be.
    card["chat_complete"] = bool((chats_doc or {}).get("complete", True))
    card["chat_pages_read"] = int((chats_doc or {}).get("pages_read") or 0)

    # Redaction happens HERE, at the boundary, so nothing downstream can skip it.
    lines: List[Dict[str, Optional[str]]] = []
    redacted_count = 0
    for row in raw_rows:
        text = row.get("chat") or ""
        if cfg["screening"]["redact_coords"]:
            text, n = redact.redact(text)
            redacted_count += n
        lines.append({"ts": row.get("time"), "chat": text})
    card["chat_lines"] = lines
    card["coords_redacted"] = redacted_count

    # ---- signals ----------------------------------------------------------
    first_seen = parse_ts((stats or {}).get("firstSeen"))
    last_seen = parse_ts((stats or {}).get("lastSeen"))
    card["first_seen"] = first_seen
    card["last_seen"] = last_seen
    card["playtime_s"] = (stats or {}).get("playtimeSeconds")

    now = _now()
    card["is_new"] = bool(
        first_seen and (now - first_seen).days <= pol["new_player_days"])

    # The claim-verifying signal: a recent death IS the "lost everything" report.
    newest_death = None
    for d in card["deaths"]:
        when = parse_ts(d.get("time"))
        if when and (newest_death is None or when > newest_death):
            newest_death = when
    card["newest_death"] = newest_death
    card["recently_died"] = bool(
        newest_death and (now - newest_death).total_seconds()
        <= pol["wipe_window_hours"] * 3600)

    # Explicitly ambiguous, never resolved into a lean. See docs/reviewing.md.
    playtime = card["playtime_s"] or 0
    card["low_playtime_old_account"] = bool(
        first_seen and (now - first_seen).days > pol["new_player_days"]
        and playtime < 3600)

    # ---- ledger -----------------------------------------------------------
    # Every ledger read is scoped to this guild: a server's kit history, flags and
    # linked-account fan-out are its own, and one server's reviewers must never see who
    # another has helped.
    card["cooldown_days"] = pol["cooldown_days"]
    # Scoped to THIS kind of request when there is one. The two cooldowns run independently:
    # being funded for a build must not stop you asking for a rescue kit when you die, and vice
    # versa. `request_type=None` -- a /lookup with no ticket -- deliberately asks about any
    # grant, because there is no one cooldown to report.
    card["cooldown"] = st.cooldown(guild_id, pol["cooldown_days"],
                                   discord_user_id=discord_user_id, mc_uuid=mc_uuid,
                                   kind=request_type)
    # History is NOT kind-scoped: only one cooldown applies, but a reviewer wants to see
    # everything this person has been given.
    card["kit_history"] = [dict(r) for r in st.kit_history(
        guild_id, discord_user_id=discord_user_id, mc_uuid=mc_uuid, limit=5)]
    card["flags"] = [dict(r) for r in st.flags_for(
        guild_id, mc_uuid=mc_uuid, mc_name=mc_name)]
    card["linked"] = [dict(r) for r in st.linked_accounts(
        guild_id, discord_user_id=discord_user_id, mc_uuid=mc_uuid)]

    # The per-identity request clock. Not kind-scoped and not outcome-scoped: one request per
    # identity per window, whatever it was for and whatever came of it. `ticket_id` excludes the
    # applicant's own row, which `create_ticket` has already written by the time this runs and
    # would otherwise be the most recent request every single time.
    #
    # Skipped entirely on a lookup. `/lookup` passes the REVIEWER's Discord id -- there is no
    # applicant -- so computing this there would put the reviewer's own request history on the
    # card as though it were the subject's, which is worse than showing nothing.
    is_lookup = request_type is None
    card["request_cooldown_days"] = 0 if is_lookup else pol["request_cooldown_days"]
    card["request_cooldown"] = st.request_cooldown(
        guild_id, card["request_cooldown_days"], discord_user_id,
        exclude_ticket_id=ticket_id)

    # The kit-farm shape: other Discord identities that have asked for THIS MC account. On a
    # lookup nothing is excluded, because "who has asked for this account" is the whole question
    # a reviewer vetting a name is asking; on a ticket the applicant is excluded so the card says
    # *other* accounts and means it.
    card["other_requesters"] = [dict(r) for r in st.other_requesters(
        guild_id, mc_uuid=mc_uuid, mc_name=mc_name,
        exclude_user_id=None if is_lookup else discord_user_id)]
    card["farmed_before"] = sum(
        int(r.get("approved") or 0) for r in card["other_requesters"])

    # ---- screening --------------------------------------------------------
    if cfg["screening"]["enabled"] and lex:
        card["screening"] = screening.summarise([l["chat"] or "" for l in lines], lex)
    else:
        card["screening"] = None

    return card


# --------------------------------------------------------------------- rendering

def kind_of(card: Dict[str, Any]) -> str:
    """The request type of a card, defaulting to rescue for a card built before types."""
    value = card.get("request_type") or store_mod.KIND_RESCUE
    return value if value in store_mod.KINDS else store_mod.KIND_RESCUE


def headline(card: Dict[str, Any]) -> str:
    """One line a reviewer can act on without reading further, when that is possible.

    Two request types read almost opposite evidence, so they get different headlines. A death
    an hour ago is the entire case for a rescue kit and says nothing at all about whether to
    fund a build; time on the server is weak evidence for a rescue and the main thing for
    funding. Leading a funding card with "died 20 minutes ago" would put the wrong fact in the
    one position a reviewer always reads.
    """
    if card["cooldown"]["blocked"]:
        return "INSIDE COOLDOWN - %d day(s) left" % card["cooldown"]["days_left"]
    rq = card.get("request_cooldown") or {}
    if rq.get("blocked"):
        return ("ALREADY REQUESTED - #%s was %s, %d day(s) left on the %d-day limit"
                % (rq.get("last_ticket_id"), rq.get("last_status"), rq["days_left"],
                   card.get("request_cooldown_days") or 0))
    if not card["tracked"]:
        return "Not tracked on 2b2t - no history at all under this account"
    bits = []
    if kind_of(card) == store_mod.KIND_FUNDING:
        if card["playtime_s"]:
            bits.append("%s played" % duration(card["playtime_s"]))
        if card["is_new"]:
            bits.append("first seen %s" % ago(card["first_seen"], card["generated_at"]))
        if card["kit_history"]:
            bits.append("%d previous grant(s)" % len(card["kit_history"]))
    else:
        if card["recently_died"]:
            bits.append("died %s" % ago(card["newest_death"], card["generated_at"]))
        if card["is_new"]:
            bits.append("first seen %s" % ago(card["first_seen"], card["generated_at"]))
    if card["flags"]:
        bits.append("%d reviewer flag(s)" % len(card["flags"]))
    if card.get("other_requesters"):
        bits.append("%d other Discord account(s) on this MC account"
                    % len(card["other_requesters"]))
    # The chat finding belongs in the one line a reviewer always reads, not only in the trace.
    scr = card.get("screening") or {}
    flagged = sum(len(v) for k, v in (scr.get("category_lines") or {}).items()
                  if k in _DECIDING_CATEGORIES)
    if flagged:
        bits.append("%d flagged chat line(s)" % flagged)
    if bits:
        return " | ".join(bits)
    # The fallback has to agree with the call. It used to say "nothing stands out" underneath a
    # heading that said Blocked, which is the card contradicting itself in the two places a
    # reviewer looks first.
    return ("Blocked - see the trace below" if recommend(card)["call"] == CALL_DENY
            else "Nothing stands out either way - read the card")


# The funding form's questions, in the order a reviewer wants them. Kept here rather than in
# bot.py so the wording of the card and the wording of the form cannot drift apart.
FUNDING_FIELDS = (
    ("project", "What it is"),
    ("needs", "What they need"),
    ("scale", "How big"),
)


def funding_section(card: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """What a funding applicant actually asked for, or None if this is a rescue request."""
    if kind_of(card) != store_mod.KIND_FUNDING:
        return None
    details = card.get("details") or {}
    rows = []
    for key, label in FUNDING_FIELDS:
        value = str(details.get(key) or "").strip()
        rows.append("**%s:** %s" % (label, value or "*(not given)*"))
    return {"name": "The ask", "value": "\n".join(rows)}


# ------------------------------------------------------------- recommendation
#
# What follows is deliberately **a rule trace, not a score**, and the distinction is the whole
# design rather than a wording preference. A score is a single number standing in for a
# judgement nobody can audit; a trace names which rule fired, on what evidence, so a reviewer
# disagrees with a specific claim in one glance. `docs/reviewing.md` and the test asserting no
# numeric score anywhere are both about the first thing, and neither is weakened by the second.
#
# **Chat can deny, and slurs are one of the things it denies on.** That is the point of reading
# chat history at all. An earlier version of this file refused both, on the reasoning that a
# false deny costs more than a false approve -- which assumed the recommendation carried weight
# by itself. It does not: a human reviews every ticket and the heading says so, so a wrong
# "Blocked" costs a reviewer disagreeing, not an applicant going without a kit.
#
# What survives from that reasoning is a *threshold*, for a different reason. Run the tuned
# lexicon over the 2025 bulk dump and 3.7% of ALL 2b2t chat matches, with 18% of accounts hitting
# at least once; a recommendation that said Blocked on one hit would say Blocked for a fifth of
# all applicants, and a heading that fires for a fifth of everyone teaches reviewers to skip it.
# So the deny needs volume behind it -- see CHAT_DENY, whose numbers are percentiles of that
# same dump rather than round guesses.
#
# One limit does survive intact: **profanity never moves the call.** It is the server's ambient
# register rather than a signal, and unlike a slur it has no target. It stays visible as a count.
#
# Thresholds are on flagged *lines*, never on hits. One line can match several terms -- "dox your
# ass" matches two -- so counting hits would let a single sentence look like a pattern.

CALL_APPROVE = "approve"
CALL_LOOK = "look"
CALL_DENY = "deny"

_CALL_RANK = {CALL_APPROVE: 0, CALL_LOOK: 1, CALL_DENY: 2}

# Categories that may influence the call at all, and what each one means when it fires. Any
# category not named here -- profanity -- is counted and displayed but never consulted.
_DECIDING_CATEGORIES = {
    "off_game": (CALL_LOOK,
                 "statements that leave the game, aimed at a person rather than a player"),
    "slur": (CALL_LOOK, "slurs, which is what reading chat history is for"),
    "scam": (CALL_LOOK,
             "scam patterns, which is a different question from swearing and is "
             "kit-relevant in its own right"),
}

# What it takes for chat alone to say deny: this many flagged LINES, or this share of the lines
# actually read, whichever comes first. Every number is a percentile of the 42,015 accounts in the
# 2025 bulk dump, so it says "how rare is this on 2b2t" rather than "how bad do I think this is".
#
# Measured, per category, as the share of ALL accounts each threshold would fire on -- counting
# what the card can actually SEE, i.e. the flagged lines expected inside a 500-line read rather
# than a year's total, because a heavy talker with 800 flagged lines in 13,000 shows the card ~30:
#
#             >=1 line    at these thresholds        one notch looser
#   slur       17.65%     15 lines 1.09% | 15% 1.26%    8 lines 2.71% | 8% 2.82%
#   off_game    1.73%      3 lines 0.06% |  3% 0.08%    2 lines 0.11% | 2% 0.13%
#
# The first column is why there is a threshold at all: nearly a fifth of the server has said
# something the slur list matches at some point in a year, so a one-hit deny would stamp Blocked
# on a fifth of all applicants and teach reviewers to skip the heading. ~1% is rare enough to
# still mean something and common enough to catch the people it is for.
#
# off_game sits far lower because it is 10x rarer and is the only category aimed at a person
# rather than at the room. It is deliberately not 1: a keyword list cannot tell a threat from a
# discussion of one, which is not hypothetical -- the top five accounts by score in this same dump
# were an advertising fleet whose ad copy contained the word "doxxing". The shipped lexicon
# carries directed forms only for that reason, which makes these counts, if anything, generous.
CHAT_DENY = {
    "off_game": {"lines": 3, "rate": 0.03},
    "slur": {"lines": 15, "rate": 0.15},
}

# A rate needs a denominator worth dividing by. Below this many lines read only the absolute line
# count can deny -- 2 flagged lines out of 3 is 67% and means nothing. 40 also sets a floor on the
# evidence: at slur's 15% it takes 6 flagged lines before the rate arm can fire at all.
#
# The dump's p75 is 55 lines in a YEAR, so this leaves the rate arm applying to roughly the
# talkative quarter and the count arm covering everyone else. The gap that leaves is deliberate:
# 12 flagged lines out of 30 read trips neither arm, and stays a "read these lines" instead.
CHAT_DENY_MIN_READ = 40


def _chat_says(category: str, flagged_lines: int, lines_read: int,
               floor: str) -> "tuple[str, str]":
    """What one category's chat hits say, and the clause explaining why.

    `floor` is the category's baseline verdict from _DECIDING_CATEGORIES; this only ever
    escalates past it, never below.
    """
    rule = CHAT_DENY.get(category)
    if not rule:
        return floor, ""
    if rule["lines"] and flagged_lines >= rule["lines"]:
        return CALL_DENY, " - %d flagged lines is past the %d this denies at" % (
            flagged_lines, rule["lines"])
    if rule["rate"] and lines_read >= CHAT_DENY_MIN_READ:
        share = flagged_lines / float(lines_read)
        if share >= rule["rate"]:
            return CALL_DENY, " - %.0f%% of the %d lines read, past the %.0f%% this denies at" % (
                100 * share, lines_read, 100 * rule["rate"])
    return floor, ""


def recommend(card: Dict[str, Any]) -> Dict[str, Any]:
    """``{'call': 'approve'|'look'|'deny', 'rules': [...], 'basis': str}``.

    `rules` is every rule that fired, each as ``{'rule', 'says', 'because'}``. The call is the
    most cautious thing any of them said, so a single deny outranks nine approves. An empty
    rule list is possible and honest: it means nothing stands out, which is what `look` means.
    """
    rules: List[Dict[str, str]] = []
    kind = kind_of(card)

    def fires(rule: str, says: str, because: str) -> None:
        rules.append({"rule": rule, "says": says, "because": because})

    # --- the mechanical denies: facts rather than inferences ---------------------
    deny_flags = [f for f in card["flags"] if str(f.get("kind")) == "deny"]
    if deny_flags:
        fires("do-not-serve flag", CALL_DENY,
              "a reviewer has flagged this account do-not-serve%s"
              % (" - \"%s\"" % deny_flags[0]["note"] if deny_flags[0].get("note") else ""))
    cd = card["cooldown"]
    if cd["blocked"]:
        fires("cooldown", CALL_DENY,
              "inside the %d-day cooldown for %s, %d day(s) left"
              % (card.get("cooldown_days", 21), store_mod.KIND_LABEL[kind], cd["days_left"]))
    rq = card.get("request_cooldown") or {}
    if rq.get("blocked"):
        fires("one request per %d days" % card.get("request_cooldown_days", 180), CALL_DENY,
              "this Discord account opened #%s %s and it was %s - %d day(s) left, and being "
              "declined is not a free retry"
              % (rq.get("last_ticket_id"), ago(at_epoch(rq.get("last_at")),
                                               card["generated_at"]),
                 rq.get("last_status"), rq.get("days_left")))
    # A second Discord identity asking for an account somebody was ALREADY GRANTED a kit on is
    # the farm, and it is a fact about the ledger rather than an inference about a person -- which
    # is why it may deny. Prior requests that were never granted are the same shape without the
    # payoff, so those only ask for a look: a friend asking on someone's behalf, an account that
    # changed hands and a farm all look identical until one of them collects.
    others = card.get("other_requesters") or []
    if others:
        who = len(others)
        matched = "uuid" if card.get("mc_uuid") else "name only"
        if card.get("farmed_before"):
            fires("kit farming", CALL_DENY,
                  "%d other Discord account(s) have requested this same Minecraft account and "
                  "%d of those request(s) were granted (matched on %s)"
                  % (who, card["farmed_before"], matched))
        else:
            fires("same MC account, different Discord", CALL_LOOK,
                  "%d other Discord account(s) have requested this Minecraft account, none "
                  "granted (matched on %s) - a farm, a friend asking for someone, and an "
                  "account that changed hands all look like this" % (who, matched))

    # --- chat: asks for a look, and denies once there is volume behind it -------
    scr = card["screening"]
    if scr:
        lines_read = len(card.get("chat_lines") or [])
        for category, (floor, why) in sorted(_DECIDING_CATEGORIES.items()):
            hits = scr["per_category"].get(category)
            if not hits:
                continue
            where = (scr.get("category_lines") or {}).get(category) or []
            says, escalation = _chat_says(category, len(where), lines_read, floor)
            fires("chat: %s" % category, says,
                  "%d hit(s) on %d line(s)%s - %s%s"
                  % (hits, len(where),
                     " (lines %s)" % ", ".join(str(w) for w in where[:8]) if where else "",
                     why, escalation))

    # --- incomplete evidence is not clean evidence -----------------------------
    if not card["deaths_ok"]:
        fires("deaths unavailable", CALL_LOOK,
              "the death lookup failed, so \"no recent death\" cannot be concluded")
    if not card["chats_ok"]:
        fires("chat unavailable", CALL_LOOK,
              "the chat lookup failed, so nothing here says their chat is clean")
    elif not card.get("chat_complete", True):
        fires("chat only sampled", CALL_LOOK,
              "%s, so a clean result covers only what was read"
              % chat_coverage_phrase(card))

    # --- the ambiguous signals the card already refuses to resolve -------------
    if not card["tracked"]:
        fires("no 2b2t history", CALL_LOOK,
              "nothing under this account at all - either brand new, or the name is wrong")
    if card["low_playtime_old_account"]:
        fires("low playtime, old account", CALL_LOOK,
              "identical for an alt and for a returning lapsed player - not evidence either "
              "way, which is why it asks rather than concludes")
    if [f for f in card["flags"] if str(f.get("kind")) == "alt"]:
        fires("known-alt flag", CALL_LOOK, "a reviewer has flagged this account a known alt")
    if len({str(r.get("mc_name")) for r in card["linked"] if r.get("mc_name")}) > 1:
        fires("shared account history", CALL_LOOK,
              "more than one Minecraft name has requested from this Discord account")

    # --- what the request itself supports --------------------------------------
    if kind == store_mod.KIND_RESCUE:
        if card["recently_died"]:
            fires("recent death", CALL_APPROVE,
                  "died %s - that IS the \"lost everything\" claim, verified"
                  % ago(card["newest_death"], card["generated_at"]))
        elif card["is_new"]:
            fires("new to 2b2t", CALL_APPROVE,
                  "first seen %s, which supports \"just getting started\""
                  % ago(card["first_seen"], card["generated_at"]))
    else:
        # Funding has no equivalent of the death line. Nothing here can verify that a project
        # is real or worth materials, so the most an established account earns is "nothing
        # against them" -- the judgement itself stays with the reviewer, deliberately.
        if card["tracked"] and (card["playtime_s"] or 0) >= 3600:
            fires("established account", CALL_APPROVE,
                  "%s played, so this is not a fresh account asking for materials"
                  % duration(card["playtime_s"]))

    call = CALL_LOOK
    if rules:
        call = max((r["says"] for r in rules), key=lambda s: _CALL_RANK[s])
    return {"call": call, "rules": rules, "basis": chat_coverage_phrase(card)}


# Phrased as what to do, not as a grade. "Looks approvable" invites agreement; "Nothing against
# them" states the finding and leaves the decision where it belongs.
_CALL_HEADING = {
    CALL_APPROVE: "Nothing against them",
    CALL_LOOK: "Worth a closer look",
    CALL_DENY: "Blocked",
}

_SAYS_MARK = {CALL_APPROVE: "✓", CALL_LOOK: "?", CALL_DENY: "✗"}

# Discord caps an embed field value at 1024 characters. This text has to fit inside that on its
# own rather than leaning on the caller to cut it: twelve rules on a heavy talker come to ~1630
# characters, and a tail-truncation there silently ate the "this is a recommendation, not the
# decision" caveat -- on Blocked cards specifically, which is the one place it has to survive.
RULES_TEXT_BUDGET = 1024


def rules_text(rec: Dict[str, Any]) -> str:
    """The rule trace as a reviewer reads it: one line per rule, marked with what it said.

    Guaranteed to fit RULES_TEXT_BUDGET. When it cannot, the *least decisive* rules go first --
    approves, then looks -- because on a Blocked card those are the lines a reviewer needs least,
    and what gets dropped is stated rather than just missing. Deny rules and the caveat always
    survive.
    """
    if rec["call"] == CALL_DENY:
        caveat = ("\n*This is a recommendation, not the decision. Approve anyway if you "
                  "disagree - the reason you type is what the ledger keeps.*")
    else:
        caveat = ("\n*Profanity counts are deliberately **not** part of this - measured "
                  "against a year of 2b2t chat they flag everybody. Slurs and off-game "
                  "lines are, once there are enough of them to be a pattern.*")

    rules = list(rec["rules"])
    if not rules:
        return "\n".join(["? Nothing fired either way. Read the card.", caveat])

    def render(keep: "List[Dict[str, str]]", dropped: int) -> str:
        out = ["%s **%s** - %s" % (_SAYS_MARK[r["says"]], r["rule"], r["because"]) for r in keep]
        if dropped:
            out.append("*+%d less decisive rule(s) not shown - the card below has them.*"
                       % dropped)
        out.append(caveat)
        return "\n".join(out)

    # Drop order: least cautious first, and within a tier the last one added, so the earliest
    # and most decisive rules are the ones that stay.
    order = sorted(range(len(rules)), key=lambda i: (_CALL_RANK[rules[i]["says"]], -i))
    text = render(rules, 0)
    for cut in range(1, len(rules)):
        if len(text) <= RULES_TEXT_BUDGET:
            return text
        drop = set(order[:cut])
        text = render([r for i, r in enumerate(rules) if i not in drop], cut)
    if len(text) > RULES_TEXT_BUDGET:
        # One surviving rule is still too long to fit. Cut the rule, never the caveat.
        room = RULES_TEXT_BUDGET - len(caveat) - 2
        head = text[:max(0, room)].rsplit(" ", 1)[0]
        text = "\n".join([head + " ...", caveat])
    return text


def chat_coverage_phrase(card: Dict[str, Any]) -> str:
    """How much chat this actually looked at, in words a reviewer can act on.

    Stated rather than implied, for the same reason a failed section never renders as an empty
    one: "no bad chat found" means two completely different things after reading 12 lines and
    after reading 12 of 2,600.
    """
    if not card["chats_ok"]:
        return "chat could not be retrieved"
    read = len(card["chat_lines"])
    days = card.get("chat_window_days") or 0
    window = "the last %d days" % days if days else "their most recent chat"
    if not read:
        return "no chat on record in %s" % window
    if card.get("chat_complete", True):
        return "read all %d line(s) from %s" % (read, window)
    total = card.get("chats_total")
    return ("read the most recent %d line(s) of %s in %s - the rest was not examined"
            % (read, "%s" % total if total else "more", window))


def sections(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """``[{'name':..., 'value':...}]``, ordered by what settles a decision fastest."""
    gen = card["generated_at"]
    out: List[Dict[str, str]] = []

    # 0. What fired, and why. First because it is a table of contents for the rest of the card
    # rather than a substitute for it: every line names the evidence it read, so the reviewer's
    # next move is to go and check that evidence.
    rec = recommend(card)
    out.append({"name": _CALL_HEADING[rec["call"]], "value": rules_text(rec)})

    ask = funding_section(card)
    if ask is not None:
        # For funding, what they asked for comes before any history: unlike a rescue kit, the
        # request itself is most of the decision. A reviewer cannot judge "is this worth
        # funding" from playtime, and reading the ask first is what stops the history being
        # weighed against a project nobody has described yet.
        out.append(ask)

    # 1. Deaths first. This is the one that usually decides it.
    if not card["deaths_ok"]:
        deaths = "could not be retrieved - **this is not the same as 'no deaths'**"
    elif not card["deaths"]:
        deaths = "none recorded"
    else:
        rows = []
        for d in card["deaths"]:
            when = ago(parse_ts(d.get("time")), gen)
            msg = (d.get("deathMessage") or "").strip() or "(no message)"
            killer = d.get("killerPlayerName") or d.get("killerMob")
            tail = " - by **%s**" % killer if killer else ""
            rows.append("- `%s` %s%s" % (when, msg, tail))
        deaths = "\n".join(rows)
        # Rescue only. On a funding request the same recent death is just something that
        # happened -- it verifies a claim nobody made, and labelling it "verified" would
        # dress up an irrelevance as a finding.
        if card["recently_died"] and kind_of(card) == store_mod.KIND_RESCUE:
            deaths = ("**A death this recent is the \"lost everything\" claim, verified.**\n"
                      + deaths)
    out.append({"name": "Recent deaths", "value": deaths})

    # 2. Time on the server.
    if card["tracked"]:
        seen = ["First seen: **%s** (%s)" % (
            card["first_seen"].strftime("%Y-%m-%d") if card["first_seen"] else "unknown",
            ago(card["first_seen"], gen))]
        seen.append("Last seen: %s" % ago(card["last_seen"], gen))
        seen.append("Playtime: %s" % duration(card["playtime_s"]))
        st = card["stats"] or {}
        seen.append("Joins %s | deaths %s | kills %s | chats %s" % (
            st.get("joinCount", "?"), st.get("deathCount", "?"),
            st.get("killCount", "?"), st.get("chatsCount", "?")))
        if card["low_playtime_old_account"]:
            seen.append(
                "\n*Low playtime on an old account. This looks identical for an alt and for "
                "a returning lapsed player - it is not evidence either way.*")
        out.append({"name": "On 2b2t", "value": "\n".join(seen)})
    else:
        out.append({"name": "On 2b2t",
                    "value": "No record. Either brand new, or the name is wrong."})

    # 3. Ledger.
    cd = card["cooldown"]
    led = []
    if cd["blocked"]:
        led.append("**Inside the %d-day cooldown - %d day(s) left**"
                   % (card.get("cooldown_days", 21), cd["days_left"]))
        led.append("Matched on: %s" % cd["matched"])
    elif cd["last_at"]:
        led.append("Last kit: %s (cooldown clear)" % ago(at_epoch(cd["last_at"]), gen))
    elif card["kit_history"]:
        # The cooldown is scoped to THIS request type; the history is not. Saying "no kit from
        # us before" directly above "kits on record: 1" is the card contradicting itself -- both
        # were true, and neither said which question it was answering.
        led.append("Nothing of this kind granted before (the cooldown is per request type).")
    else:
        led.append("No kit from us before.")
    if card["kit_history"]:
        led.append("Kits on record: %d (all types)" % len(card["kit_history"]))

    rq = card.get("request_cooldown") or {}
    days = card.get("request_cooldown_days") or 0
    if rq.get("blocked"):
        led.append("**One request per %d days: %d day(s) left** (#%s, %s, %s)"
                   % (days, rq["days_left"], rq["last_ticket_id"], rq["last_status"],
                      ago(at_epoch(rq["last_at"]), gen)))
    elif rq.get("last_at"):
        led.append("Previous request: #%s, %s, %s" % (
            rq["last_ticket_id"], rq["last_status"], ago(at_epoch(rq["last_at"]), gen)))
    elif days:
        led.append("First request from this Discord account.")

    others = card.get("other_requesters") or []
    if others:
        led.append("")
        led.append("**%d other Discord account(s) have requested this Minecraft account**%s"
                   % (len(others),
                      " - %d of those were granted" % card["farmed_before"]
                      if card.get("farmed_before") else ", none granted"))
        for r in others[:5]:
            led.append("- <@%s> - %s ticket(s), last %s%s"
                       % (r.get("discord_user_id"), r.get("tickets"),
                          ago(at_epoch(r.get("newest")), gen),
                          ", %s approved" % r["approved"] if r.get("approved") else ""))
        if not card.get("mc_uuid"):
            led.append("*Matched on name only - no UUID resolved, so this is weaker than usual.*")
    if len(card["linked"]) > 1:
        names = sorted({str(r.get("mc_name")) for r in card["linked"]
                        if r.get("mc_name")})
        if len(names) > 1:
            led.append("Other names on this Discord account: %s" % ", ".join(names[:6]))
            led.append("*Shared account history is what an alt looks like - and also a "
                       "rename, a sibling, or a borrowed account.*")
    out.append({"name": "Kit ledger", "value": "\n".join(led)})

    # 4. Reviewer flags.
    if card["flags"]:
        out.append({"name": "Reviewer flags", "value": "\n".join(
            "- **%s**%s" % (f.get("kind"), " - " + f["note"] if f.get("note") else "")
            for f in card["flags"])})

    # 5. Chat.
    chat = []
    if not card["chats_ok"]:
        chat.append("could not be retrieved - **not the same as 'said nothing'**")
    else:
        chat.append("%d recent line(s) - press **Chat history** to read them%s." % (
            len(card["chat_lines"]),
            " of %s total" % card["chats_total"] if card["chats_total"] else ""))
        if card["coords_redacted"]:
            chat.append("%d coordinate-like value(s) hidden before display."
                        % card["coords_redacted"])
        scr = card["screening"]
        if scr is None:
            chat.append("No lexicon loaded - nothing counted, read the lines.")
        elif not scr["per_category"]:
            chat.append("Lexicon matched nothing across %d line(s)." % scr["lines_scanned"])
        else:
            parts = ", ".join("%s=%d" % (k, v)
                             for k, v in sorted(scr["per_category"].items()))
            chat.append("Matches: %s (in %d of %d lines)"
                        % (parts, scr["lines_flagged"], scr["lines_scanned"]))
            chat.append("*Counts, not a verdict. Profanity is the server's ambient "
                        "register; what matters is whether a statement leaves the game.*")
    out.append({"name": "Recent public chat", "value": "\n".join(chat)})

    if card["errors"]:
        out.append({"name": "Incomplete", "value": "\n".join(
            "- %s" % e for e in card["errors"])})

    return out


# ---------------------------------------------------------------- meeting point

# Standalone runs of digits only. The lookarounds are what stop "2b2t" contributing a 2: a
# match may not begin or end against a word character, so the 2 in "2b2t" is rejected and
# "2b2t spawn 100 64 -200" still reads as (100, 64, -200) rather than (2, 2, 100). A decimal
# is truncated the same way -- "100.5" yields 100 and the .5 cannot start its own match.
_COORD = re.compile(r"(?<![\w.])(-?\d+)(?![\w])")

# Generous rather than exact. Y is checked at all only to catch a transposed pair like
# "100 -200 64", and the bounds cover every world height Minecraft has had.
_XZ_LIMIT = 30_000_000
_Y_RANGE = (-256, 512)

DIMENSIONS = ("overworld", "nether", "end")


def parse_coords(text: Optional[str]) -> Optional[tuple]:
    """``(x, y, z)`` out of whatever the applicant typed, or None if it isn't three numbers.

    Accepts every shape people actually type -- ``100 64 -200``, ``100, 64, -200``,
    ``x=100 y=64 z=-200``, ``100/64/-200`` -- because rejecting a real answer over punctuation
    would strand somebody who is waiting on a delivery.
    """
    if not text:
        return None
    found = _COORD.findall(text.replace("−", "-").replace("–", "-"))
    if len(found) < 3:
        return None
    try:
        x, y, z = (int(n) for n in found[:3])
    except ValueError:                                  # pragma: no cover - regex guarantees
        return None
    if abs(x) > _XZ_LIMIT or abs(z) > _XZ_LIMIT:
        return None
    if not (_Y_RANGE[0] <= y <= _Y_RANGE[1]):
        return None
    return (x, y, z)


def format_coords(coords: Optional[tuple], dimension: Optional[str] = None) -> str:
    if not coords:
        return "not given yet"
    out = "`%d %d %d`" % coords
    if dimension:
        out += " (%s)" % dimension
    return out


# ------------------------------------------------------------------- chat pager
#
# Paging the chat log inside Discord instead of attaching a .txt a reviewer has to download.
# All of it lives here, in the module that imports no Discord, for one concrete reason: the
# packing has to be **deterministic**. The page a reviewer is looking at is not remembered
# anywhere -- it is re-derived from the ledger rows on every button press -- so the same rows
# must always split into the same pages. That makes it a pure function over a list, and a pure
# function belongs where it can be tested without a gateway or a database.

# The embed *description* holds the body: it caps at 4096 against a field's 1024, so using a
# field would quadruple the page count for nothing. 3800 leaves room for the code fence.
CHAT_PAGE_BODY_BUDGET = 3800
# Readability, not an API limit. Twenty lines is about a screen.
CHAT_PAGE_MAX_LINES = 20
_STAMP_W = 11                                        # "MM-DD HH:MM"


def neutralise(text: Optional[str]) -> str:
    """Make one chat line safe to drop inside a code fence, without altering what it says.

    The fence itself handles almost everything -- ``*``, ``_``, ``~``, ``@everyone``, ``<@1>``
    all render literally inside one -- which matters because a reviewer is judging the exact
    text: letting Discord italicise ``__Notch__`` into *Notch* silently edits the evidence.

    Two things the fence cannot defend against, so they are handled here:
    a backtick can close the fence early and spill the rest of the page into live markdown, and
    a newline would make one stored line render as two, which would break the position numbers
    that `/flagline` matches on.
    """
    out = (text or "").replace("\r", " ").replace("\n", " ")
    out = out.replace("​", "")                  # no invisible space to hide inside
    out = "".join(c if (c >= " " or c == "\t") else " " for c in out)
    # Backtick + zero-width space: visually identical, cannot terminate the fence. A reviewer
    # copy-pasting such a line carries the ZWSP with it, which is the lesser problem.
    return out.replace("`", "`​")


def _stamp(ts: Optional[str]) -> str:
    raw = ts or ""
    return "%s %s" % (raw[5:10], raw[11:16]) if len(raw) >= 16 else " " * _STAMP_W


# A code block inside an *embed description* gets no horizontal scrollbar -- Discord wraps it at
# whatever the container width happens to be. So a long chat line broke at column 0 and ran
# underneath the next entry's number, which is what made the list hard to read. There is no way
# to make it expand; the fix is to put the break somewhere we choose and indent the continuation
# to line up under the text. 72 is a compromise: comfortable on desktop, and a narrow client
# re-wraps only the few lines that are still too long, with the indent already correct.
CHAT_PAGE_WRAP = 72


def _wrap(body: str, room: int) -> List[str]:
    """`body` split to `room` columns, at spaces where there is one.

    The space a break lands on is kept on the end of the line before it, so no character of what
    somebody said is dropped -- the whole point of `neutralise` is that the evidence a reviewer
    reads is the evidence that exists, and silently eating a space is still an edit. A token
    longer than `room` (a 9,000-character paste) has nowhere to break and is cut.
    """
    room = max(8, room)
    out: List[str] = []
    while len(body) > room:
        cut = body.rfind(" ", 0, room + 1)
        if cut <= 0:
            out.append(body[:room])
            body = body[room:]
        else:
            out.append(body[:cut + 1])
            body = body[cut + 1:]
    out.append(body)
    return out


def chat_page_chunks(row: Any, budget: int = CHAT_PAGE_BODY_BUDGET,
                     pos_width: int = 4, wrap: int = CHAT_PAGE_WRAP) -> List[str]:
    """One ledger row rendered, wrapped to `wrap` columns with a hanging indent.

    The flag column marks a line a reviewer flagged with `/flagline`. Positions are printed from
    ``row["position"]`` and never re-derived from the loop index: `record_shown_chats` numbers
    from zero and `flag_chat` matches that exact integer, so re-numbering per page would offset
    every `/flagline` by the page offset and mislabel the only screening data that exists. They
    repeat on every continuation line, because a row long enough to be split can be split across
    a page boundary too, and a continuation that does not say which position it belongs to is
    unreadable on the page that starts with it.

    `pos_width` comes from the widest position in the whole log rather than being fixed at 4, so
    a 20-line log does not carry three columns of empty space in front of every entry.
    """
    # Left-aligned, so the padding sits to the RIGHT of the number instead of in front of it.
    # Right-aligning put up to three blank columns before every entry on a short log, which is
    # the empty left margin this format exists to not have. Every later column still lines up.
    flag = "!" if row["flagged"] else " "
    head = "%s%-*d %s " % (flag, pos_width, int(row["position"]), _stamp(row["chat_ts"]))
    cont = "%s%-*d %s " % (flag, pos_width, int(row["position"]), " " * _STAMP_W)
    # Never wider than a page can hold, whatever `wrap` says.
    room = min(max(8, wrap - len(head)), budget - len(head) - 1)
    parts = _wrap(neutralise(row["chat"]), room)
    return ["%s%s" % (head if i == 0 else cont, part) for i, part in enumerate(parts)]


def chat_pages(rows: List[Any], budget: int = CHAT_PAGE_BODY_BUDGET,
               max_lines: int = CHAT_PAGE_MAX_LINES) -> List[Dict[str, Any]]:
    """Ledger rows (in `position` order) -> pages.

    Each page is ``{'lines': [str], 'flagged': [position], 'first': int, 'last': int}``.
    Always at least one page, so a caller never has to special-case an empty list.

    Every chunk out of `chat_page_chunks` is at most `budget` long by construction, so the
    flush-then-append below always lands a chunk in an empty page and the loop cannot spin.
    """
    pages: List[Dict[str, Any]] = []
    lines: List[str] = []
    flagged: List[int] = []
    seen: List[int] = []
    used = 0

    # Widest position in the WHOLE log, not per page: the number column has to sit in the same
    # place on every page or paging through them makes the text jump sideways.
    pos_width = max([len(str(int(r["position"]))) for r in rows] or [1])

    for row in rows:
        position = int(row["position"])
        for text in chat_page_chunks(row, budget, pos_width):
            need = len(text) + 1                     # the newline that will join it
            if lines and (used + need > budget or len(lines) >= max_lines):
                pages.append({"lines": lines, "flagged": flagged,
                              "first": seen[0], "last": seen[-1]})
                lines, flagged, seen, used = [], [], [], 0
            lines.append(text)
            used += need
            if position not in seen:
                seen.append(position)
            if row["flagged"] and position not in flagged:
                flagged.append(position)
    if lines:
        pages.append({"lines": lines, "flagged": flagged,
                      "first": seen[0], "last": seen[-1]})
    return pages or [{"lines": [], "flagged": [], "first": 0, "last": 0}]


# Deliberately does not say "they said nothing". The ledger cannot tell an applicant who was
# silent from an api.2b2t.vc call that failed -- `chat_lines` is empty in both cases -- and
# asserting silence would be the worse of the two wrong answers. The card's "Recent public
# chat" field does distinguish them, so this points there instead of guessing.
CHAT_PAGE_EMPTY = (
    "No chat lines are recorded for this ticket.\n\n"
    "Whether that means the player has said nothing in public chat or the lookup could not "
    "be completed is on the card's **Recent public chat** field - the ledger does not record "
    "which.")


def chat_page_body(page: Dict[str, Any]) -> str:
    """The page as an embed description: one fenced block, or the empty note."""
    if not page["lines"]:
        return CHAT_PAGE_EMPTY
    return "```\n%s\n```" % "\n".join(page["lines"])


def chat_page_footer(pages: List[Dict[str, Any]], index: int, line_count: int,
                     ticket_id: int) -> str:
    page = pages[index]
    where = ("lines %d-%d of %d" % (page["first"], page["last"], line_count)
             if page["lines"] else "nothing recorded")
    return "Page %d/%d · %s · flag with /flagline %d <numbers>" % (
        index + 1, len(pages), where, ticket_id)


def chat_file(rows: List[Any], ticket_id: int, mc_name: str) -> str:
    """The pager's download, rebuilt from ledger rows rather than from a live card.

    Same shape as `chat_dump` so a reviewer used to the old attachment reads the same thing,
    but sourced from `shown_chats` -- which means it still works months later, long after the
    card that produced it stopped existing anywhere.
    """
    head = [
        "Chat shown to reviewers for ticket #%d (%s)" % (ticket_id, mc_name),
        "Rebuilt from the ledger, %d line(s). Coordinate-like values were already replaced"
        % len(rows),
        "with %s before storage." % redact.PLACEHOLDER,
        "A leading ! marks a line a reviewer flagged with /flagline.",
        "Line numbers are the positions /flagline matches on.",
        "-" * 72,
    ]
    body = ["%s%4d  %s  %s" % ("! " if r["flagged"] else "  ", int(r["position"]),
                               (r["chat_ts"] or "")[:19].replace("T", " "), r["chat"] or "")
            for r in rows]
    return "\n".join(head + (body or ["(no chat on record)"])) + "\n"


def chat_dump(card: Dict[str, Any]) -> str:
    """The attached, already-redacted chat log. Line numbers match `flagline` positions."""
    head = [
        "Recent public chat for %s" % card["mc_name"],
        "UUID: %s" % (card["mc_uuid"] or "unresolved"),
        "Retrieved: %s" % card["generated_at"].strftime("%Y-%m-%d %H:%M:%SZ"),
        "Coordinate-like values replaced with %s (%d substitution(s))."
        % (redact.PLACEHOLDER, card["coords_redacted"]),
        "Line numbers below are the positions used by /melonkit flagline.",
        "-" * 72,
    ]
    body = []
    for i, row in enumerate(card["chat_lines"]):
        ts = (row.get("ts") or "")[:19].replace("T", " ")
        body.append("%4d  %s  %s" % (i, ts, row.get("chat") or ""))
    if not body:
        body.append("(no chat on record)")
    return "\n".join(head + body) + "\n"
