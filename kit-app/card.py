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
           details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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

    chats_doc = attempt("chats", lambda: client.chats(
        uuid=mc_uuid, name=mc_name, limit=pol["recent_chats"]))
    raw_rows = (chats_doc or {}).get("chats") or []
    card["chats_total"] = (chats_doc or {}).get("total")
    card["chats_ok"] = chats_doc is not None

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
    return " | ".join(bits) if bits else "Nothing stands out either way - read the card"


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


def sections(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """``[{'name':..., 'value':...}]``, ordered by what settles a decision fastest."""
    gen = card["generated_at"]
    out: List[Dict[str, str]] = []

    # 0. For funding, what they asked for comes before any history: unlike a rescue kit, the
    # request itself is most of the decision. A reviewer cannot judge "is this worth funding"
    # from playtime, and reading the ask first is what stops the history being weighed against
    # a project nobody has described yet.
    ask = funding_section(card)
    if ask is not None:
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
        led.append("Last kit: %s (cooldown clear)" % ago(
            datetime.datetime.fromtimestamp(cd["last_at"], datetime.timezone.utc), gen))
    else:
        led.append("No kit from us before.")
    if card["kit_history"]:
        led.append("Kits on record: %d" % len(card["kit_history"]))
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


def chat_page_chunks(row: Any, budget: int = CHAT_PAGE_BODY_BUDGET) -> List[str]:
    """One ledger row rendered, split further only if it cannot fit a page by itself.

    The gutter marks a line a reviewer flagged with `/flagline`. Positions are printed from
    ``row["position"]`` and never re-derived from the loop index: `record_shown_chats` numbers
    from zero and `flag_chat` matches that exact integer, so re-numbering per page would offset
    every `/flagline` by the page offset and mislabel the only screening data that exists.
    """
    gutter = "! " if row["flagged"] else "  "
    head = "%s%4d %s  " % (gutter, int(row["position"]), _stamp(row["chat_ts"]))
    cont = "%s%4d %s  " % (gutter, int(row["position"]), " " * _STAMP_W)
    body = neutralise(row["chat"])
    room = max(16, budget - len(head) - 1)
    if len(body) <= room:
        return ["%s%s" % (head, body)]
    out, rest = ["%s%s…" % (head, body[:room])], body[room:]
    while rest:
        room = max(16, budget - len(cont) - 1)
        out.append("%s…%s" % (cont, rest[:room]))
        rest = rest[room:]
    return out


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

    for row in rows:
        position = int(row["position"])
        for text in chat_page_chunks(row, budget):
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
