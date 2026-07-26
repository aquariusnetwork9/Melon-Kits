"""Wipe ticket history from the ledger, and optionally the Discord objects with it.

For clearing test data. **This deletes kit history, which is the one thing in this project
that cannot be regenerated**, so it takes a timestamped backup of the database file first and
prints where it went.

    python deploy/reset_tickets.py --config melonkit.json --dry-run
    python deploy/reset_tickets.py --config melonkit.json --yes
    python deploy/reset_tickets.py --config melonkit.json --yes --delete-discord

Reviewer flags are kept by default: they are accumulated human knowledge about accounts, not
ticket history, and losing them costs more than it saves. `--flags-too` drops them as well.

Run as the service user, never under sudo -- see DEPLOY.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as config_mod   # noqa: E402

TICKET_TABLES = ("shown_chats", "decisions", "kits", "tickets")


def summarise(db):
    out = {}
    for t in TICKET_TABLES + ("flags",):
        out[t] = db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    return out


async def delete_discord(cfg, targets):
    """Delete the applicant threads and queue posts belonging to the wiped tickets."""
    import discord
    token = os.environ.get(cfg["discord"]["token_env"])
    if not token:
        print("  no token in environment, skipping Discord cleanup")
        return
    client = discord.Client(intents=discord.Intents.default())
    done = {"n": 0, "missing": 0}

    @client.event
    async def on_ready():
        try:
            for label, cid in targets:
                try:
                    ch = client.get_channel(cid) or await client.fetch_channel(cid)
                    await ch.delete()
                    print("  deleted %s %s" % (label, cid))
                    done["n"] += 1
                except discord.NotFound:
                    done["missing"] += 1
                except discord.HTTPException as exc:
                    print("  could not delete %s %s: %s" % (label, cid, exc))
        finally:
            await client.close()

    await client.start(token)
    print("  deleted %d, already gone %d" % (done["n"], done["missing"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--yes", action="store_true", help="actually do it")
    ap.add_argument("--dry-run", action="store_true", help="show what would go")
    ap.add_argument("--delete-discord", action="store_true",
                    help="also delete the applicant threads and queue posts")
    ap.add_argument("--flags-too", action="store_true",
                    help="also drop reviewer flags (kept by default)")
    args = ap.parse_args(argv)

    cfg = config_mod.load_config(args.config)
    path = cfg["store"]["path"]
    if not os.path.exists(path):
        print("no ledger at %s -- nothing to clear" % path)
        return 0

    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    before = summarise(db)
    print("ledger: %s" % path)
    for t, n in before.items():
        print("  %-12s %d rows%s" % (t, n, "  (kept)" if t == "flags"
                                     and not args.flags_too else ""))

    targets = []
    for r in db.execute("SELECT id, thread_id, queue_thread_id FROM tickets"):
        if r["thread_id"]:
            targets.append(("applicant thread (ticket %s)" % r["id"], int(r["thread_id"])))
        if r["queue_thread_id"]:
            targets.append(("queue post (ticket %s)" % r["id"], int(r["queue_thread_id"])))
    print("\nDiscord objects referenced: %d" % len(targets))

    if args.dry_run or not args.yes:
        print("\nnothing changed. Re-run with --yes to apply%s."
              % (" (and --delete-discord to remove the threads/posts)"
                 if targets else ""))
        return 0

    backup = "%s.bak-%d" % (path, int(time.time()))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold the WAL in before copying
    shutil.copy2(path, backup)
    print("\nbackup written: %s" % backup)

    if args.delete_discord and targets:
        print("\ndeleting Discord objects:")
        asyncio.run(delete_discord(cfg, targets))

    db.execute("BEGIN")
    try:
        for t in TICKET_TABLES:
            db.execute("DELETE FROM %s" % t)
        if args.flags_too:
            db.execute("DELETE FROM flags")
        # Restart the id counters so the next ticket is #1 again -- this is a reset, and
        # ticket numbers carrying over from deleted test data is just confusing.
        db.execute("DELETE FROM sqlite_sequence WHERE name IN "
                   "('tickets','decisions','kits','shown_chats','flags')")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    db.execute("VACUUM")

    after = summarise(db)
    print("\nafter:")
    for t, n in after.items():
        print("  %-12s %d rows" % (t, n))
    print("\nnext ticket will be #1. Backup is at %s if you need any of it back." % backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
