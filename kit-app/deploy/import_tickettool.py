"""Seed the ledger from the Ticket Tool transcripts that predate this bot.

WHY THIS EXISTS
    Repeat farming is the abuse this project actually guards against, and the guard is the
    ledger: "have we helped this person before". On a server with years of history behind it,
    a ledger that starts empty means every long-standing repeat requester reads as brand new
    on day one. This imports that history so the bot arrives already knowing.

WHAT THE DATA LOOKS LIKE
    Ticket Tool posts one message per closed ticket into a log channel: an embed naming the
    ticket owner, and an **attached HTML transcript**. The transcript is not the rendered page
    it looks like -- the conversation is a base64 JSON blob in `let messages = "..."`, which is
    good news, because it means every ticket is recoverable from Discord alone with no
    third-party fetching.

    The Minecraft name is NOT a structured field. Ticket Tool's opening embed asks people to
    state it in prose, so it arrives as free text: "My Minecraft username is X", "my in game
    name is X". So this extracts candidates by pattern and then **validates them against
    Mojang** -- a name that does not resolve to a real account is discarded. That check is what
    makes a fuzzy parse safe: the cost of a wrong guess is a stranger inheriting somebody
    else's kit history, and guessing is not acceptable there.

SAFETY
    - Redaction runs at ingest, on every string taken from a transcript, before anything is
      stored or printed. These are 2b2t support tickets; they are full of base coordinates.
    - Idempotent on the source message id, so a re-run skips what it already did. A
      double-imported kit is a real person wrongly refused for having "already had two".
    - Original timestamps throughout. Cooldowns are computed from dates, so importing a year
      of history stamped `now` would put the whole community on cooldown on day one.
    - `--dry-run` is the default. Nothing is written until you pass `--write`.

USAGE
    python deploy/import_tickettool.py --config melonkit.json --guild <id> \
        --channels 1348463090491981824,1416495527389827145 [--limit 50] [--write]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as config_mod   # noqa: E402
import identity               # noqa: E402
import redact as redact_mod   # noqa: E402
import store as store_mod     # noqa: E402
import tickettool             # noqa: E402


def log(msg: str) -> None:
    sys.stderr.write("  %s\n" % msg)


def owner_from_embeds(message: discord.Message) -> Tuple[Optional[int], Optional[str]]:
    """(ticket owner id, ticket name) from Ticket Tool's log embed."""
    pairs = []
    for e in message.embeds:
        for f in e.fields:
            pairs.append((f.name, f.value))
    return tickettool.owner_from_fields(pairs)


class Transient(Exception):
    """The lookup could not be completed. Try this ticket again on the next run."""


class Importer(object):
    def __init__(self, cfg, st, guild_id: int, write: bool, resolve: bool):
        self.cfg = cfg
        self.st = st
        self.guild_id = guild_id
        self.write = write
        self.resolve = resolve
        self.uuid_cache: Dict[str, Optional[Tuple[str, str]]] = {}
        self.stats = {
            "messages": 0, "transcripts": 0, "skipped_existing": 0, "no_owner": 0,
            "no_candidate": 0, "unresolved": 0, "imported": 0, "kits": 0,
            "deferred": 0, "lookup_errors": 0,
        }

    def resolve_name(self, name: str) -> Optional[Tuple[str, str]]:
        """(canonical name, uuid) from Mojang, or None. Cached; paced for the rate limit.

        Raises `Transient` when the lookup could not be *completed*, which is deliberately not
        the same as the name not existing. "No such account" is a real answer and the ticket
        can be marked done; "Mojang did not respond" is not, and treating it as one would burn
        through a thousand tickets during an outage, recording every single one as permanently
        unresolvable with no way to tell them apart afterwards.
        """
        key = name.lower()
        if key in self.uuid_cache:
            return self.uuid_cache[key]
        result = None
        if self.resolve:
            if not identity.valid_name(name):
                self.uuid_cache[key] = None
                return None
            try:
                res = identity.resolve(name, self.cfg)
                if res and res.get("uuid"):
                    result = (res.get("name") or name, res["uuid"])
            except identity.UnknownPlayer:
                result = None                              # definitive: not a real account
            except identity.IdentityError as exc:
                self.stats["lookup_errors"] += 1
                raise Transient(str(exc))
            finally:
                time.sleep(1.1)                            # Mojang is not generous
        self.uuid_cache[key] = result
        return result

    async def handle(self, message: discord.Message) -> None:
        self.stats["messages"] += 1
        att = next((a for a in message.attachments if a.filename.endswith(".html")), None)
        if att is None:
            return
        self.stats["transcripts"] += 1

        if self.st.already_imported(message.id):
            self.stats["skipped_existing"] += 1
            return

        owner, ticket_name = owner_from_embeds(message)
        if owner is None:
            self.stats["no_owner"] += 1
            return

        try:
            raw = (await att.read()).decode("utf-8", "replace")
        except discord.HTTPException as exc:
            log("could not download %s: %s" % (att.filename, getattr(exc, "status", "?")))
            return
        entries = tickettool.decode_transcript(raw)
        names = tickettool.candidate_names(entries, owner, redactor=redact_mod.redact)
        when = int(message.created_at.timestamp())

        if not names:
            self.stats["no_candidate"] += 1
            if self.write:
                # Recorded even with no name: it still says a ticket existed, and it stops a
                # re-run downloading this transcript again for nothing.
                self.st.record_import(message.id, self.guild_id, "tickettool",
                                      None, None, None)
            return

        resolved = None
        try:
            for cand in names[:3]:
                resolved = self.resolve_name(cand)
                if resolved:
                    break
        except Transient as exc:
            # Leave no import row, so the next run picks this ticket up again.
            self.stats["deferred"] += 1
            if self.stats["deferred"] == 1:
                log("deferring tickets - name lookups are failing (%s)" % exc)
            return
        if not resolved:
            self.stats["unresolved"] += 1
            if self.write:
                self.st.record_import(message.id, self.guild_id, "tickettool",
                                      None, None, None)
            return

        canonical, uuid = resolved
        self.stats["imported"] += 1
        note = "imported from Ticket Tool %s" % (ticket_name or "ticket")
        if not self.write:
            return
        tid = self.st.create_historical_ticket(
            self.guild_id, owner, canonical, uuid, when, note=note)
        # A kit row is what answers "have we helped this person before" -- and it is only
        # written when the account was confirmed, so an unreadable ticket never invents a grant.
        kid = self.st.record_historical_kit(
            self.guild_id, tid, owner, canonical, uuid, when)
        self.stats["kits"] += 1
        self.st.record_import(message.id, self.guild_id, "tickettool", tid, kid, canonical)


async def run(cfg, args) -> int:
    st = store_mod.open_store(cfg["store"]["path"])
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        sys.exit("no token in %s" % cfg["discord"]["token_env"])

    client = discord.Client(intents=discord.Intents.default())
    imp = Importer(cfg, st, args.guild, args.write, not args.no_resolve)

    @client.event
    async def on_ready():
        try:
            for cid in [int(c) for c in args.channels.split(",") if c.strip()]:
                ch = await client.fetch_channel(cid)
                log("sweeping #%s" % ch.name)
                n = 0
                async for message in ch.history(limit=args.limit, oldest_first=True):
                    await imp.handle(message)
                    n += 1
                    if n % 100 == 0:
                        log("  ...%d messages, %d imported" % (n, imp.stats["imported"]))
        except Exception as exc:                            # noqa: BLE001
            log("sweep failed: %s: %s" % (type(exc).__name__, exc))
        finally:
            await client.close()

    await client.start(token)
    st.close()

    print()
    print("%s" % ("WROTE TO THE LEDGER" if args.write else "DRY RUN - nothing written"))
    for k in ("messages", "transcripts", "skipped_existing", "no_owner", "no_candidate",
              "unresolved", "deferred", "lookup_errors", "imported", "kits"):
        print("  %-18s %d" % (k, imp.stats[k]))
    got = imp.stats["imported"]
    seen = max(1, imp.stats["transcripts"] - imp.stats["skipped_existing"])
    print("  coverage           %.1f%% of transcripts yielded a confirmed account"
          % (100.0 * got / seen))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--guild", type=int, required=True,
                    help="guild id the history belongs to - MUST be the server it is for")
    ap.add_argument("--channels", required=True, help="comma-separated channel ids")
    ap.add_argument("--limit", type=int, default=None, help="messages per channel")
    ap.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip the Mojang check - faster, and imports nothing verifiable")
    args = ap.parse_args()
    cfg = config_mod.load_config(args.config)
    sys.exit(asyncio.run(run(cfg, args)))


if __name__ == "__main__":
    main()
