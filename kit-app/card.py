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
    if secs < 3600:
        return "%d minutes" % (secs // 60)
    if secs < 86400 * 2:
        return "%.1f hours" % (secs / 3600.0)
    return "%.1f days (%d hours)" % (secs / 86400.0, secs // 3600)


# --------------------------------------------------------------------- gathering

def gather(mc_name: str, mc_uuid: Optional[str], discord_user_id: int,
           cfg: Dict[str, Any], client: "vc_mod.Client", st: "store_mod.Store",
           lex: Optional[screening.Lexicon] = None,
           log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Everything the card needs. Partial failures degrade rather than abort.

    A review must not be impossible because one endpoint is briefly rate-limited: whatever
    was retrieved still goes in front of the reviewer, and whatever failed says so out loud
    rather than rendering as an innocent-looking zero. A missing death list and an empty
    death list mean opposite things.
    """
    log = log or _LOG
    pol = cfg["policy"]
    card: Dict[str, Any] = {
        "mc_name": mc_name,
        "mc_uuid": mc_uuid,
        "discord_user_id": int(discord_user_id),
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
    card["cooldown_days"] = pol["cooldown_days"]
    card["cooldown"] = st.cooldown(pol["cooldown_days"],
                                  discord_user_id=discord_user_id, mc_uuid=mc_uuid)
    card["kit_history"] = [dict(r) for r in st.kit_history(
        discord_user_id=discord_user_id, mc_uuid=mc_uuid, limit=5)]
    card["flags"] = [dict(r) for r in st.flags_for(mc_uuid=mc_uuid, mc_name=mc_name)]
    card["linked"] = [dict(r) for r in st.linked_accounts(
        discord_user_id=discord_user_id, mc_uuid=mc_uuid)]

    # ---- screening --------------------------------------------------------
    if cfg["screening"]["enabled"] and lex:
        card["screening"] = screening.summarise([l["chat"] or "" for l in lines], lex)
    else:
        card["screening"] = None

    return card


# --------------------------------------------------------------------- rendering

def headline(card: Dict[str, Any]) -> str:
    """One line a reviewer can act on without reading further, when that is possible."""
    if card["cooldown"]["blocked"]:
        return "INSIDE COOLDOWN - %d day(s) left" % card["cooldown"]["days_left"]
    if not card["tracked"]:
        return "Not tracked on 2b2t - no history at all under this account"
    bits = []
    if card["recently_died"]:
        bits.append("died %s" % ago(card["newest_death"], card["generated_at"]))
    if card["is_new"]:
        bits.append("first seen %s" % ago(card["first_seen"], card["generated_at"]))
    if card["flags"]:
        bits.append("%d reviewer flag(s)" % len(card["flags"]))
    return " | ".join(bits) if bits else "Nothing stands out either way - read the card"


def sections(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """``[{'name':..., 'value':...}]``, ordered by what settles a decision fastest."""
    gen = card["generated_at"]
    out: List[Dict[str, str]] = []

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
        if card["recently_died"]:
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
        chat.append("%d recent line(s) attached%s." % (
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
