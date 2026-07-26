"""The kit ledger: SQLite, one file, no server.

The ledger is the whole anti-abuse surface. **The abuse case is repeat farming, not
toxicity** (docs/reviewing.md), so what has to be accurate is "has this person had a kit
recently", and everything else here is in service of that or of the instrumentation below.

Cooldown is checked against **both** the Discord user id and the Minecraft UUID, because
either one alone is trivially sidestepped -- a second Discord account with the same MC
account, or a second MC account on the same Discord. Neither is airtight (nothing detects a
genuinely separate person) and it does not need to be: the kits are disposable.

**Instrumentation.** `record_shown_chats` and `flag_chat` exist because production is the
only place labelled screening data comes from and it cannot be backfilled -- which chat lines
a reviewer saw and which ones they objected to is a judgement that exists at the moment it
is made and nowhere else. Chat is stored **already redacted** (redact.redact runs before it
gets here) so the ledger never becomes the coordinate leak the display path avoids being.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = 2

STATUS_OPEN = "open"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"
STATUS_CANCELLED = "cancelled"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER NOT NULL,
    mc_name         TEXT    NOT NULL,
    mc_uuid         TEXT,
    -- The applicant-facing private thread: conversation only, never the reviewer card.
    thread_id       INTEGER,
    -- The staff forum post holding the card, the decision and the claim. A forum post's
    -- starter message shares the thread's id, so this one column locates both.
    queue_thread_id INTEGER,
    status          TEXT    NOT NULL,
    note            TEXT,
    created_at      INTEGER NOT NULL,
    closed_at       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tickets_user   ON tickets(discord_user_id, status);
CREATE INDEX IF NOT EXISTS ix_tickets_uuid   ON tickets(mc_uuid);
CREATE INDEX IF NOT EXISTS ix_tickets_thread ON tickets(thread_id);
-- ix_tickets_queue is created in _migrate(), NOT here. This script runs before the column
-- migration, so indexing queue_thread_id at this point fails outright on a database written
-- by a version that predates the column.

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    reviewer_id INTEGER NOT NULL,
    outcome     TEXT    NOT NULL,
    reason      TEXT,
    decided_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_ticket ON decisions(ticket_id);

-- The ledger proper: one row per kit that actually went out.
CREATE TABLE IF NOT EXISTS kits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER REFERENCES tickets(id),
    discord_user_id INTEGER NOT NULL,
    mc_uuid         TEXT,
    mc_name         TEXT    NOT NULL,
    claimed_by      INTEGER,
    claimed_at      INTEGER,
    delivered_at    INTEGER,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kits_user ON kits(discord_user_id);
CREATE INDEX IF NOT EXISTS ix_kits_uuid ON kits(mc_uuid);

-- Reviewer-maintained flags. 'Known alt' is a list, never a computation: nothing
-- distinguishes an alt from a returning lapsed player, so this outlives the reviewer who
-- recognised the name instead of being re-derived badly.
CREATE TABLE IF NOT EXISTS flags (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT    NOT NULL,
    mc_uuid  TEXT,
    mc_name  TEXT,
    note     TEXT,
    set_by   INTEGER NOT NULL,
    set_at   INTEGER NOT NULL,
    cleared_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_flags_uuid ON flags(mc_uuid, kind);
CREATE INDEX IF NOT EXISTS ix_flags_name ON flags(mc_name, kind);

-- Instrumentation. Impossible to backfill; see the module docstring.
CREATE TABLE IF NOT EXISTS shown_chats (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    position  INTEGER NOT NULL,
    chat_ts   TEXT,
    chat      TEXT    NOT NULL,     -- already coordinate-redacted
    flagged   INTEGER NOT NULL DEFAULT 0,
    shown_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shown_ticket ON shown_chats(ticket_id);
"""


def _now() -> int:
    return int(time.time())


class Store(object):
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._migrate()
        cur = self._db.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                             (str(SCHEMA_VERSION),))
        elif int(row["value"]) < SCHEMA_VERSION:
            self._db.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                             (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(
                "ledger at %s was written by a newer version (schema %s > %s); refusing to "
                "open it read-write rather than risk mangling the kit history"
                % (path, row["value"], SCHEMA_VERSION))

    def _migrate(self) -> None:
        """Additive column migrations.

        CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so a database
        made by an older version keeps the older columns and every later query referencing a
        new one fails at runtime rather than at startup. Adding them here means an upgrade
        never needs the ledger rebuilt -- which matters because the ledger is the one thing
        in this project that cannot be regenerated.
        """
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(tickets)")}
        if "queue_thread_id" not in have:
            self._db.execute("ALTER TABLE tickets ADD COLUMN queue_thread_id INTEGER")
        # Unconditional, and outside the branch above: on a fresh database the column comes
        # from CREATE TABLE, so a branch-local index would never be created there.
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tickets_queue ON tickets(queue_thread_id)")

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ tickets

    def open_ticket_for(self, discord_user_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tickets WHERE discord_user_id=? AND status=? "
            "ORDER BY id DESC LIMIT 1",
            (int(discord_user_id), STATUS_OPEN)).fetchone()

    def open_ticket_count(self, discord_user_id: int) -> int:
        return int(self._db.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE discord_user_id=? AND status=?",
            (int(discord_user_id), STATUS_OPEN)).fetchone()["n"])

    def create_ticket(self, discord_user_id: int, mc_name: str,
                      mc_uuid: Optional[str] = None,
                      note: Optional[str] = None) -> int:
        cur = self._db.execute(
            "INSERT INTO tickets(discord_user_id, mc_name, mc_uuid, status, note, "
            "created_at) VALUES(?,?,?,?,?,?)",
            (int(discord_user_id), mc_name, mc_uuid, STATUS_OPEN, note, _now()))
        return int(cur.lastrowid)

    def set_ticket_thread(self, ticket_id: int, thread_id: int) -> None:
        self._db.execute("UPDATE tickets SET thread_id=? WHERE id=?",
                         (int(thread_id), int(ticket_id)))

    def set_queue_thread(self, ticket_id: int, queue_thread_id: int) -> None:
        self._db.execute("UPDATE tickets SET queue_thread_id=? WHERE id=?",
                         (int(queue_thread_id), int(ticket_id)))

    def ticket_for_queue_thread(self, queue_thread_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tickets WHERE queue_thread_id=? ORDER BY id DESC LIMIT 1",
            (int(queue_thread_id),)).fetchone()

    def set_ticket_uuid(self, ticket_id: int, mc_uuid: Optional[str],
                        mc_name: Optional[str] = None) -> None:
        if mc_name:
            self._db.execute("UPDATE tickets SET mc_uuid=?, mc_name=? WHERE id=?",
                             (mc_uuid, mc_name, int(ticket_id)))
        else:
            self._db.execute("UPDATE tickets SET mc_uuid=? WHERE id=?",
                             (mc_uuid, int(ticket_id)))

    def get_ticket(self, ticket_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM tickets WHERE id=?",
                                (int(ticket_id),)).fetchone()

    def ticket_for_thread(self, thread_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tickets WHERE thread_id=? ORDER BY id DESC LIMIT 1",
            (int(thread_id),)).fetchone()

    # ---------------------------------------------------------------- decisions

    def record_decision(self, ticket_id: int, reviewer_id: int, outcome: str,
                        reason: Optional[str] = None) -> None:
        if outcome not in (STATUS_APPROVED, STATUS_DECLINED, STATUS_CANCELLED):
            raise ValueError("unknown outcome %r" % (outcome,))
        now = _now()
        self._db.execute("BEGIN")
        try:
            self._db.execute(
                "INSERT INTO decisions(ticket_id, reviewer_id, outcome, reason, decided_at)"
                " VALUES(?,?,?,?,?)",
                (int(ticket_id), int(reviewer_id), outcome, reason, now))
            self._db.execute("UPDATE tickets SET status=?, closed_at=? WHERE id=?",
                             (outcome, now, int(ticket_id)))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def decisions_for(self, ticket_id: int) -> List[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT * FROM decisions WHERE ticket_id=? ORDER BY id", (int(ticket_id),)))

    # -------------------------------------------------------------------- kits

    def record_kit(self, ticket_id: Optional[int], discord_user_id: int, mc_name: str,
                   mc_uuid: Optional[str]) -> int:
        cur = self._db.execute(
            "INSERT INTO kits(ticket_id, discord_user_id, mc_uuid, mc_name, created_at) "
            "VALUES(?,?,?,?,?)",
            (ticket_id, int(discord_user_id), mc_uuid, mc_name, _now()))
        return int(cur.lastrowid)

    def claim_kit(self, kit_id: int, claimed_by: int) -> bool:
        """Claim a dispatch. Returns False if somebody already had it.

        A conditional UPDATE rather than check-then-set: two runners pressing Claim at the
        same moment is the normal case, not a rare one, and the loser has to be told.
        """
        cur = self._db.execute(
            "UPDATE kits SET claimed_by=?, claimed_at=? WHERE id=? AND claimed_by IS NULL",
            (int(claimed_by), _now(), int(kit_id)))
        return cur.rowcount > 0

    def unclaim_kit(self, kit_id: int, actor_id: int) -> bool:
        """Hand a delivery back, but only by whoever took it."""
        cur = self._db.execute(
            "UPDATE kits SET claimed_by=NULL, claimed_at=NULL "
            "WHERE id=? AND claimed_by=? AND delivered_at IS NULL",
            (int(kit_id), int(actor_id)))
        return cur.rowcount > 0

    def release_kit(self, kit_id: int) -> bool:
        """Force a delivery back into the pool regardless of who holds it.

        Separate from `unclaim_kit` rather than an `actor_id=None` special case: a reviewer
        prising a stale claim off someone who has gone quiet is a different act from a runner
        handing back their own, and a flag on one function would make the two indis-
        tinguishable at the call site.
        """
        cur = self._db.execute(
            "UPDATE kits SET claimed_by=NULL, claimed_at=NULL "
            "WHERE id=? AND delivered_at IS NULL", (int(kit_id),))
        return cur.rowcount > 0

    def mark_delivered(self, kit_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE kits SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
            (_now(), int(kit_id)))
        return cur.rowcount > 0

    def get_kit(self, kit_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM kits WHERE id=?", (int(kit_id),)).fetchone()

    # ---------------------------------------------------------------- cooldown

    def last_kit(self, discord_user_id: Optional[int] = None,
                 mc_uuid: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Most recent kit matching either identifier. Either alone is easy to sidestep."""
        clauses, args = [], []
        if discord_user_id is not None:
            clauses.append("discord_user_id=?")
            args.append(int(discord_user_id))
        if mc_uuid:
            clauses.append("mc_uuid=?")
            args.append(mc_uuid)
        if not clauses:
            return None
        return self._db.execute(
            "SELECT * FROM kits WHERE %s ORDER BY created_at DESC LIMIT 1"
            % (" OR ".join(clauses),), args).fetchone()

    def cooldown(self, cooldown_days: int, discord_user_id: Optional[int] = None,
                 mc_uuid: Optional[str] = None, now: Optional[int] = None) -> Dict[str, Any]:
        """``{'blocked': bool, 'days_left': int, 'last_at': int|None, 'matched': str|None}``"""
        row = self.last_kit(discord_user_id, mc_uuid)
        if row is None or cooldown_days <= 0:
            return {"blocked": False, "days_left": 0, "last_at": None, "matched": None}
        now = _now() if now is None else int(now)
        elapsed = now - int(row["created_at"])
        window = cooldown_days * 86400
        matched = "mc_uuid" if (mc_uuid and row["mc_uuid"] == mc_uuid) else "discord_user"
        if elapsed >= window:
            return {"blocked": False, "days_left": 0,
                    "last_at": int(row["created_at"]), "matched": matched}
        # Ceiling, so "1 day left" never means "come back in 40 minutes".
        days_left = -(-(window - elapsed) // 86400)
        return {"blocked": True, "days_left": int(days_left),
                "last_at": int(row["created_at"]), "matched": matched}

    def kit_history(self, discord_user_id: Optional[int] = None,
                    mc_uuid: Optional[str] = None, limit: int = 10) -> List[sqlite3.Row]:
        clauses, args = [], []
        if discord_user_id is not None:
            clauses.append("discord_user_id=?")
            args.append(int(discord_user_id))
        if mc_uuid:
            clauses.append("mc_uuid=?")
            args.append(mc_uuid)
        if not clauses:
            return []
        args.append(int(limit))
        return list(self._db.execute(
            "SELECT * FROM kits WHERE %s ORDER BY created_at DESC LIMIT ?"
            % (" OR ".join(clauses),), args))

    def linked_accounts(self, discord_user_id: Optional[int] = None,
                        mc_uuid: Optional[str] = None) -> List[sqlite3.Row]:
        """Other identities that have shared a ticket with these -- the ledger fan-out.

        Evidence for a reviewer, not a verdict: sharing a Discord account with another MC
        name is what an alt looks like *and* what a sibling, a rename, or a borrowed account
        looks like.
        """
        if discord_user_id is None and not mc_uuid:
            return []
        return list(self._db.execute(
            "SELECT DISTINCT discord_user_id, mc_uuid, mc_name FROM tickets "
            "WHERE (discord_user_id=? OR mc_uuid=?) ORDER BY id DESC LIMIT 25",
            (int(discord_user_id) if discord_user_id is not None else -1, mc_uuid or "")))

    # ------------------------------------------------------------------- flags

    def set_flag(self, kind: str, set_by: int, mc_uuid: Optional[str] = None,
                 mc_name: Optional[str] = None, note: Optional[str] = None) -> int:
        if not mc_uuid and not mc_name:
            raise ValueError("a flag needs a uuid or a name")
        cur = self._db.execute(
            "INSERT INTO flags(kind, mc_uuid, mc_name, note, set_by, set_at) "
            "VALUES(?,?,?,?,?,?)", (kind, mc_uuid, mc_name, note, int(set_by), _now()))
        return int(cur.lastrowid)

    def clear_flag(self, flag_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE flags SET cleared_at=? WHERE id=? AND cleared_at IS NULL",
            (_now(), int(flag_id)))
        return cur.rowcount > 0

    def flags_for(self, mc_uuid: Optional[str] = None,
                  mc_name: Optional[str] = None) -> List[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT * FROM flags WHERE cleared_at IS NULL AND "
            "(mc_uuid IS NOT NULL AND mc_uuid=? OR mc_name IS NOT NULL AND "
            "LOWER(mc_name)=LOWER(?)) ORDER BY id DESC",
            (mc_uuid or "", mc_name or "")))

    # --------------------------------------------------------- instrumentation

    def record_shown_chats(self, ticket_id: int,
                           lines: Sequence[Dict[str, Optional[str]]]) -> None:
        """`lines` are ``{'ts':..., 'chat':...}`` with `chat` ALREADY redacted."""
        now = _now()
        self._db.execute("BEGIN")
        try:
            self._db.executemany(
                "INSERT INTO shown_chats(ticket_id, position, chat_ts, chat, shown_at) "
                "VALUES(?,?,?,?,?)",
                [(int(ticket_id), i, row.get("ts"), row.get("chat") or "", now)
                 for i, row in enumerate(lines)])
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def flag_chat(self, ticket_id: int, position: int, flagged: bool = True) -> bool:
        cur = self._db.execute(
            "UPDATE shown_chats SET flagged=? WHERE ticket_id=? AND position=?",
            (1 if flagged else 0, int(ticket_id), int(position)))
        return cur.rowcount > 0

    def shown_chats(self, ticket_id: int) -> List[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT * FROM shown_chats WHERE ticket_id=? ORDER BY position",
            (int(ticket_id),)))

    # ------------------------------------------------------------------- stats

    def counts(self) -> Dict[str, int]:
        out = {}
        for table in ("tickets", "decisions", "kits", "flags", "shown_chats"):
            out[table] = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"])
        out["flagged_chats"] = int(self._db.execute(
            "SELECT COUNT(*) AS n FROM shown_chats WHERE flagged=1").fetchone()["n"])
        return out


def open_store(path: str) -> Store:
    return Store(path)
