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

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = 7

STATUS_OPEN = "open"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"
STATUS_CANCELLED = "cancelled"

# What was asked for. Two different requests that happen to share a queue: someone who just
# lost everything wants a kit today, and someone building wants materials for a project. They
# are judged on almost opposite evidence -- a recent death is the whole case for the first and
# irrelevant to the second -- so the type travels with the ticket rather than being guessed
# from its contents.
KIND_RESCUE = "rescue"
KIND_FUNDING = "funding"
KINDS = (KIND_RESCUE, KIND_FUNDING)

KIND_LABEL = {KIND_RESCUE: "rescue kit", KIND_FUNDING: "project funding"}

# How a claim ended. Recorded rather than derived: once a dispatch has been through several
# hands, "handed back" and "taken off them" read identically from the surviving columns, and
# the difference is the only thing that says whether a runner was unreliable or unavailable.
CLAIM_DELIVERED = "delivered"
CLAIM_HANDED_BACK = "handed_back"
CLAIM_RELEASED = "released"

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
    -- 'rescue' or 'funding'. Defaulted rather than nullable, and the same default is used by
    -- the migration: every ticket written before this column existed was a rescue request,
    -- because that was the only thing the bot could take. So the backfill is not a guess.
    request_type    TEXT    NOT NULL DEFAULT 'rescue',
    -- Answers to the questions that only one type asks, as JSON. A funding request has a
    -- project, a materials list and a scale; a rescue request has none of them. Columns per
    -- field would mean an ALTER for every question anyone ever wants to add, and most rows
    -- would carry the ones that do not apply to them -- the same reasoning as guild_config.
    details         TEXT,
    -- Where to meet, filled in by the applicant after their request is approved. Its own
    -- column rather than a key in `details` because it is written at a different time, by a
    -- different person's action, and read by the runner rather than the reviewer.
    -- Stored as the applicant typed it AND parsed, so a runner sees a canonical "x y z" while
    -- anything extra they wrote ("by the big cobble tower") is not thrown away.
    meet_coords     TEXT,
    meet_dimension  TEXT,
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
    -- Which cooldown this grant starts. The two kinds are tracked separately on purpose:
    -- being funded for a build should not stop you asking for a rescue kit when you die,
    -- and being rescued should not stop you asking for materials. Without this column the
    -- cooldown query cannot tell the two apart and every grant blocks both.
    kind            TEXT    NOT NULL DEFAULT 'rescue',
    claimed_by      INTEGER,
    claimed_at      INTEGER,
    delivered_at    INTEGER,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kits_user ON kits(discord_user_id);
CREATE INDEX IF NOT EXISTS ix_kits_uuid ON kits(mc_uuid);

-- Every claim a dispatch has ever had, including the ones handed back. `kits.claimed_by` is
-- current state and is nulled on hand-back, so by itself it cannot answer "who has had this
-- delivery" -- which is exactly the question worth asking when a kit passes through three
-- runners and never arrives. Append-only: a row is closed, never deleted.
CREATE TABLE IF NOT EXISTS kit_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kit_id      INTEGER NOT NULL REFERENCES kits(id),
    actor_id    INTEGER NOT NULL,
    claimed_at  INTEGER NOT NULL,
    -- NULL while the claim is live; set when it ends, however it ends.
    released_at INTEGER,
    -- Who ended it: the holder handing back, or a reviewer prising it off someone gone quiet.
    -- Equal to actor_id for a hand-back and different for a release, which is the whole
    -- distinction between the two.
    released_by INTEGER,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS ix_kit_claims_kit ON kit_claims(kit_id);

-- Provenance for history imported from somewhere other than this bot -- the Ticket Tool
-- transcripts that predate it. Keyed on the SOURCE message id, which is what makes the
-- importer safe to re-run: a second pass finds the row and skips it. Without this a re-run
-- would double every historical grant, and a double-counted kit is a real person wrongly
-- refused for having "already had two".
CREATE TABLE IF NOT EXISTS imported_tickets (
    source_message_id INTEGER PRIMARY KEY,
    guild_id          INTEGER NOT NULL,
    source            TEXT    NOT NULL,     -- 'tickettool' | 'role'
    ticket_id         INTEGER REFERENCES tickets(id),
    kit_id            INTEGER REFERENCES kits(id),
    mc_name           TEXT,
    imported_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_imported_guild ON imported_tickets(guild_id, source);

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

-- Per-guild settings. This is what makes the bot installable rather than deployed: channel
-- and role ids, and any policy a server overrides, live here instead of in melonkit.json.
-- Key/value with JSON values rather than a wide table, so adding a setting needs no
-- migration -- the alternative is an ALTER for every knob anyone ever wants per server.
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,          -- JSON
    set_at   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, key)
);

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


def ticket_kind(row: Any) -> str:
    """The request type of a ticket row, tolerating a row that predates the column.

    Every path that reads a ticket goes through here rather than touching `request_type`
    directly. The migration fills the column in on every existing row, so in a live database
    this never falls back -- but a Row handed in by a test fixture or an older tool may not
    carry the key at all, and a KeyError from sqlite3.Row is not catchable by `.get`.
    """
    try:
        value = row["request_type"]
    except (IndexError, KeyError, TypeError):
        return KIND_RESCUE
    return value if value in KINDS else KIND_RESCUE


def ticket_details(row: Any) -> Dict[str, Any]:
    """The type-specific answers stored on a ticket, or ``{}`` if it has none.

    Never raises. A ticket whose JSON is unreadable is still a ticket someone is waiting on, so
    a reviewer gets the rest of the card rather than an error.
    """
    try:
        raw = row["details"]
    except (IndexError, KeyError, TypeError):
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Store(object):
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # One connection PER THREAD. The bot runs every blocking call -- the 2b2t API
        # requests, and the ledger reads inside card.gather -- through
        # loop.run_in_executor, so the store is genuinely touched from worker threads, and
        # a single shared connection raises
        # "SQLite objects created in a thread can only be used in that same thread".
        # WAL mode makes multiple connections to one file the normal, supported case.
        self._local = threading.local()
        self._conns = []
        self._conns_lock = threading.Lock()
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

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False alongside the thread-local: the guard is redundant once
        # every thread has its own connection, and disabling it is what lets close() tidy
        # up connections belonging to worker threads that have already gone away.
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None,
                             check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")      # persistent, but harmless to re-assert
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")       # per-connection, so this one matters
        return db

    @property
    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._connect()
            self._local.db = db
            with self._conns_lock:
                self._conns.append(db)
        return db

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

        # v3: multi-guild. Only the three tables that are queried directly need the column --
        # decisions and shown_chats are child rows reached through ticket_id, and scoping them
        # too would just be a second place for the two to disagree.
        for table in ("tickets", "kits", "flags"):
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(%s)" % table)}
            if "guild_id" not in cols:
                self._db.execute("ALTER TABLE %s ADD COLUMN guild_id INTEGER" % table)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tickets_guild ON tickets(guild_id, discord_user_id, status)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_kits_guild ON kits(guild_id, discord_user_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_flags_guild ON flags(guild_id, kind)")

        # v4: rescue kits and project funding. Unlike guild_id, these columns need no
        # adopt_legacy_rows pass -- a NOT NULL column added with a DEFAULT is written into
        # every existing row by SQLite itself, and 'rescue' is the correct value for all of
        # them rather than a plausible one: it was the only kind of request the bot could
        # accept before this migration.
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(tickets)")}
        if "request_type" not in cols:
            self._db.execute(
                "ALTER TABLE tickets ADD COLUMN request_type TEXT NOT NULL DEFAULT 'rescue'")
        if "details" not in cols:
            self._db.execute("ALTER TABLE tickets ADD COLUMN details TEXT")
        # Nullable, unlike request_type: NULL genuinely means "not given yet", which is the
        # normal state of every ticket until its applicant answers.
        if "meet_coords" not in cols:
            self._db.execute("ALTER TABLE tickets ADD COLUMN meet_coords TEXT")
        if "meet_dimension" not in cols:
            self._db.execute("ALTER TABLE tickets ADD COLUMN meet_dimension TEXT")
        # Purging. `transcript_at` is what makes deleting a thread safe: with both the applicant
        # thread and the queue post removed, the archive transcript is the ONLY record left in
        # Discord, so nothing may be deleted until one has demonstrably been posted. Backfilled
        # as NULL, which is the honest answer for tickets closed before this existed -- the
        # sweeper will write a transcript for those before it touches anything.
        if "transcript_at" not in cols:
            self._db.execute("ALTER TABLE tickets ADD COLUMN transcript_at INTEGER")
        # Kept rather than inferred from the thread ids being NULL, so "this vanished from
        # Discord on purpose, on this date" is answerable a year later.
        if "purged_at" not in cols:
            self._db.execute("ALTER TABLE tickets ADD COLUMN purged_at INTEGER")
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(kits)")}
        if "kind" not in cols:
            self._db.execute("ALTER TABLE kits ADD COLUMN kind TEXT NOT NULL DEFAULT 'rescue'")
        # Unconditional and outside the branches, for the same reason as ix_tickets_queue.
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_kits_kind ON kits(guild_id, kind, created_at)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tickets_kind ON tickets(guild_id, request_type)")

        # v6: the claim log. The table itself comes from _SCHEMA, but a dispatch claimed by an
        # older build is IN FLIGHT right now and has no row -- so it would deliver against an
        # empty history, and the transcript would report "never claimed" for a kit somebody is
        # holding as the upgrade happens. Backfilling the open claims is what makes the upgrade
        # invisible to the people mid-delivery. Idempotent via NOT EXISTS, so re-running is
        # harmless; delivered kits are left alone because their history is already closed and
        # inventing a claim row for them would be a guess, not a record.
        self._db.execute(
            "INSERT INTO kit_claims(kit_id, actor_id, claimed_at) "
            "SELECT id, claimed_by, COALESCE(claimed_at, created_at) FROM kits "
            "WHERE claimed_by IS NOT NULL AND delivered_at IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM kit_claims c WHERE c.kit_id = kits.id)")

    def adopt_legacy_rows(self, guild_id: int) -> int:
        """Stamp pre-multi-guild rows with the guild they must have belonged to.

        A single-guild deployment upgrading in place has rows with guild_id NULL, and every
        scoped query would silently skip them -- cooldowns would reset and flags would vanish,
        which looks exactly like the ledger having been wiped. Called once at startup with the
        guild adopted from melonkit.json.
        """
        total = 0
        for table in ("tickets", "kits", "flags"):
            cur = self._db.execute(
                "UPDATE %s SET guild_id=? WHERE guild_id IS NULL" % table, (int(guild_id),))
            total += cur.rowcount
        return total

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for db in conns:
            try:
                db.close()
            except Exception:
                pass
        self._local = threading.local()

    # ------------------------------------------------------------- guild config

    def get_guild_config(self, guild_id: int) -> Dict[str, Any]:
        """Everything this guild has set. ``{}`` for a guild that has never run /setup."""
        out: Dict[str, Any] = {}
        for row in self._db.execute(
                "SELECT key, value FROM guild_config WHERE guild_id=?", (int(guild_id),)):
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                # A hand-edited row should not take the guild down; ignore and carry on.
                continue
        return out

    def set_guild_config(self, guild_id: int, values: Dict[str, Any]) -> None:
        now = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for key, val in values.items():
                self._db.execute(
                    "INSERT INTO guild_config(guild_id, key, value, set_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value, "
                    "set_at=excluded.set_at",
                    (int(guild_id), str(key), json.dumps(val), now))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def clear_guild_config(self, guild_id: int) -> int:
        cur = self._db.execute("DELETE FROM guild_config WHERE guild_id=?", (int(guild_id),))
        return cur.rowcount

    def configured_guilds(self) -> List[int]:
        return [int(r["guild_id"]) for r in self._db.execute(
            "SELECT DISTINCT guild_id FROM guild_config ORDER BY guild_id")]

    # ------------------------------------------------------------------ tickets
    #
    # Everything below takes guild_id FIRST, deliberately. Row ids are globally unique, so a
    # lookup by id alone succeeds across guilds -- which would mean `/close 5` in one server
    # closing another server's ticket #5. Making the scope the first positional argument is
    # what stops it being the one that gets forgotten.

    def open_ticket_for(self, guild_id: int,
                        discord_user_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tickets WHERE guild_id=? AND discord_user_id=? AND status=? "
            "ORDER BY id DESC LIMIT 1",
            (int(guild_id), int(discord_user_id), STATUS_OPEN)).fetchone()

    def open_ticket_count(self, guild_id: int, discord_user_id: int) -> int:
        return int(self._db.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE guild_id=? AND discord_user_id=? "
            "AND status=?",
            (int(guild_id), int(discord_user_id), STATUS_OPEN)).fetchone()["n"])

    def create_ticket(self, guild_id: int, discord_user_id: int, mc_name: str,
                      mc_uuid: Optional[str] = None,
                      note: Optional[str] = None,
                      request_type: str = KIND_RESCUE,
                      details: Optional[Dict[str, Any]] = None) -> int:
        """Open a ticket. `details` is the type-specific answers, stored as JSON.

        `request_type` defaults to rescue rather than being required, so that a caller written
        before the two kinds existed -- including the tests -- keeps meaning what it meant.
        """
        if request_type not in KINDS:
            raise ValueError("unknown request_type %r" % (request_type,))
        cur = self._db.execute(
            "INSERT INTO tickets(guild_id, discord_user_id, mc_name, mc_uuid, status, note, "
            "request_type, details, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(guild_id), int(discord_user_id), mc_name, mc_uuid, STATUS_OPEN, note,
             request_type, json.dumps(details) if details else None, _now()))
        return int(cur.lastrowid)

    def set_meet_coords(self, guild_id: int, ticket_id: int, coords: str,
                        dimension: Optional[str] = None) -> bool:
        """Record where the applicant wants to meet. Returns False if there's no such ticket.

        Guild-scoped and overwriting on purpose: the applicant supplies this from a button in
        their own thread, and somebody who has moved since being approved needs to be able to
        say so rather than being stuck with the first answer.
        """
        cur = self._db.execute(
            "UPDATE tickets SET meet_coords=?, meet_dimension=? WHERE id=? AND guild_id=?",
            (coords, dimension, int(ticket_id), int(guild_id)))
        return cur.rowcount > 0

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

    def get_ticket(self, ticket_id: int,
                   guild_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        """Pass `guild_id` from anything a user can name a ticket number to.

        Without it this is a cross-tenant hole: ids are globally unique, so `/close 5` run in
        one server would happily close a different server's ticket #5. It stays optional only
        for internal callers that already hold a row they trust.
        """
        if guild_id is None:
            return self._db.execute("SELECT * FROM tickets WHERE id=?",
                                    (int(ticket_id),)).fetchone()
        return self._db.execute("SELECT * FROM tickets WHERE id=? AND guild_id=?",
                                (int(ticket_id), int(guild_id))).fetchone()

    def ticket_for_thread(self, thread_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM tickets WHERE thread_id=? ORDER BY id DESC LIMIT 1",
            (int(thread_id),)).fetchone()

    # ---------------------------------------------------------------- decisions

    def record_decision(self, ticket_id: int, reviewer_id: int, outcome: str,
                        reason: Optional[str] = None) -> bool:
        """Decide a ticket. Returns False if it was **already** decided.

        The status change is a conditional UPDATE inside the transaction, not a separate
        read-then-write, so two reviewers pressing Approve at the same instant cannot both
        win. That is not a hypothetical: without it both callers pass an `if status == open`
        check, both record a kit, and the applicant's 21-day cooldown is burned twice for one
        request. The loser gets False and is told, exactly like the Claim button.
        """
        if outcome not in (STATUS_APPROVED, STATUS_DECLINED, STATUS_CANCELLED):
            raise ValueError("unknown outcome %r" % (outcome,))
        now = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "UPDATE tickets SET status=?, closed_at=? WHERE id=? AND status=?",
                (outcome, now, int(ticket_id), STATUS_OPEN))
            if cur.rowcount == 0:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute(
                "INSERT INTO decisions(ticket_id, reviewer_id, outcome, reason, decided_at)"
                " VALUES(?,?,?,?,?)",
                (int(ticket_id), int(reviewer_id), outcome, reason, now))
            self._db.execute("COMMIT")
            return True
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def kits_for_ticket(self, ticket_id: int) -> List[sqlite3.Row]:
        """Kits belonging to one ticket.

        Used instead of filtering kit_history, which is capped at a row limit and ordered by
        recency -- for a heavy repeat applicant the relevant kit could fall off the end and
        the transcript would silently report no delivery.
        """
        return list(self._db.execute(
            "SELECT * FROM kits WHERE ticket_id=? ORDER BY id", (int(ticket_id),)))

    def decisions_for(self, ticket_id: int) -> List[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT * FROM decisions WHERE ticket_id=? ORDER BY id", (int(ticket_id),)))

    # -------------------------------------------------------------------- kits

    def record_kit(self, guild_id: int, ticket_id: Optional[int], discord_user_id: int,
                   mc_name: str, mc_uuid: Optional[str],
                   kind: str = KIND_RESCUE) -> int:
        if kind not in KINDS:
            raise ValueError("unknown kit kind %r" % (kind,))
        cur = self._db.execute(
            "INSERT INTO kits(guild_id, ticket_id, discord_user_id, mc_uuid, mc_name, kind, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (int(guild_id), ticket_id, int(discord_user_id), mc_uuid, mc_name, kind, _now()))
        return int(cur.lastrowid)

    def claim_kit(self, kit_id: int, claimed_by: int) -> bool:
        """Claim a dispatch. Returns False if somebody already had it.

        A conditional UPDATE rather than check-then-set: two runners pressing Claim at the
        same moment is the normal case, not a rare one, and the loser has to be told. The
        `kit_claims` row is written in the same transaction, so the history can never record a
        claim that did not win, nor miss one that did.
        """
        now = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # `delivered_at IS NULL` matters as much as the claim check. A delivered kit
            # normally still carries its claimer, so it cannot be re-claimed -- but a hand-back
            # racing a delivery leaves one delivered AND unclaimed, and without this guard the
            # next person to press Claim takes a kit that has already gone out. Their claim row
            # could then never be closed, because every path that closes one refuses a
            # delivered kit, so they would hold it in the ledger forever.
            cur = self._db.execute(
                "UPDATE kits SET claimed_by=?, claimed_at=? "
                "WHERE id=? AND claimed_by IS NULL AND delivered_at IS NULL",
                (int(claimed_by), now, int(kit_id)))
            if cur.rowcount == 0:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute(
                "INSERT INTO kit_claims(kit_id, actor_id, claimed_at) VALUES(?,?,?)",
                (int(kit_id), int(claimed_by), now))
            self._db.execute("COMMIT")
            return True
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def _close_claim(self, kit_id: int, released_by: Optional[int], outcome: str) -> int:
        """Close whichever claim row is still open on this kit. Returns how many it closed.

        Called inside an open transaction by every path that ends a claim. Deliberately
        forgiving about finding nothing: kits claimed before this table existed have no open
        row, and refusing to deliver one of those would be a migration that breaks live work.
        The count is returned so a caller can tell "closed a claim" from "there was none",
        which is the difference between an honest history and a silent gap in it.
        """
        cur = self._db.execute(
            "UPDATE kit_claims SET released_at=?, released_by=?, outcome=? "
            "WHERE kit_id=? AND released_at IS NULL",
            (_now(), None if released_by is None else int(released_by),
             outcome, int(kit_id)))
        return cur.rowcount

    def unclaim_kit(self, kit_id: int, actor_id: int) -> bool:
        """Hand a delivery back, but only by whoever took it."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "UPDATE kits SET claimed_by=NULL, claimed_at=NULL "
                "WHERE id=? AND claimed_by=? AND delivered_at IS NULL",
                (int(kit_id), int(actor_id)))
            if cur.rowcount == 0:
                self._db.execute("ROLLBACK")
                return False
            self._close_claim(kit_id, actor_id, CLAIM_HANDED_BACK)
            self._db.execute("COMMIT")
            return True
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def release_kit(self, kit_id: int, released_by: Optional[int] = None) -> bool:
        """Force a delivery back into the pool regardless of who holds it.

        Separate from `unclaim_kit` rather than an `actor_id=None` special case: a reviewer
        prising a stale claim off someone who has gone quiet is a different act from a runner
        handing back their own, and a flag on one function would make the two indis-
        tinguishable at the call site.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # `claimed_by IS NOT NULL` matters: without it the UPDATE matches a kit nobody
            # holds -- setting NULL to NULL -- so the call reported success for releasing
            # nothing, and now would also write a "released" outcome into the claim log for a
            # claim that never existed. Callers guard this today; the guard belongs here.
            cur = self._db.execute(
                "UPDATE kits SET claimed_by=NULL, claimed_at=NULL "
                "WHERE id=? AND claimed_by IS NOT NULL AND delivered_at IS NULL",
                (int(kit_id),))
            if cur.rowcount == 0:
                self._db.execute("ROLLBACK")
                return False
            self._close_claim(kit_id, released_by, CLAIM_RELEASED)
            self._db.execute("COMMIT")
            return True
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def mark_delivered(self, kit_id: int, delivered_by: Optional[int] = None) -> bool:
        """Record a kit as gone out, and make sure somebody is named for it.

        `delivered_by` is who pressed the button. It is needed because a delivery does not
        always have an open claim to close: a kit claimed by a build older than the claim log
        has none, and a hand-back landing in the same instant as a delivery closes the only one
        there was. Both used to leave a delivered kit whose history said nobody ever had it,
        which is worse than a slightly approximate row -- so one is written.
        """
        now = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT claimed_by, claimed_at FROM kits WHERE id=? AND delivered_at IS NULL",
                (int(kit_id),)).fetchone()
            if row is None:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute(
                "UPDATE kits SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
                (now, int(kit_id)))
            if not self._close_claim(kit_id, None, CLAIM_DELIVERED):
                # Nothing open to close. Name whoever the kit says held it, falling back to
                # whoever pressed Delivered, rather than recording a delivery by no one.
                actor = row["claimed_by"] if row["claimed_by"] is not None else delivered_by
                if actor is not None:
                    self._db.execute(
                        "INSERT INTO kit_claims(kit_id, actor_id, claimed_at, released_at, "
                        "outcome) VALUES(?,?,?,?,?)",
                        (int(kit_id), int(actor),
                         int(row["claimed_at"] or now), now, CLAIM_DELIVERED))
            self._db.execute("COMMIT")
            return True
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def claims_for_kit(self, kit_id: int,
                       guild_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Everyone who has ever held this dispatch, oldest first.

        Takes a guild like every other lookup a dispatch number can reach: ids are globally
        unique, so an unscoped read of a number a user typed or pressed is a cross-tenant hole.
        `kit_claims` has no guild of its own, so the scope comes from the kit it belongs to.
        """
        if guild_id is None:
            return list(self._db.execute(
                "SELECT * FROM kit_claims WHERE kit_id=? ORDER BY id", (int(kit_id),)))
        return list(self._db.execute(
            "SELECT c.* FROM kit_claims c JOIN kits k ON k.id = c.kit_id "
            "WHERE c.kit_id=? AND k.guild_id=? ORDER BY c.id",
            (int(kit_id), int(guild_id))))

    # ------------------------------------------------------------------ imports

    def already_imported(self, source_message_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM imported_tickets WHERE source_message_id=?",
            (int(source_message_id),)).fetchone()

    def record_import(self, source_message_id: int, guild_id: int, source: str,
                      ticket_id: Optional[int], kit_id: Optional[int],
                      mc_name: Optional[str]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO imported_tickets(source_message_id, guild_id, source, "
            "ticket_id, kit_id, mc_name, imported_at) VALUES(?,?,?,?,?,?,?)",
            (int(source_message_id), int(guild_id), source, ticket_id, kit_id,
             mc_name, _now()))

    def import_summary(self, guild_id: int) -> Dict[str, int]:
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COUNT(mc_name) AS named, COUNT(kit_id) AS kits "
            "FROM imported_tickets WHERE guild_id=?", (int(guild_id),)).fetchone()
        return {"rows": row["n"], "with_mc_name": row["named"], "with_kit": row["kits"]}

    def create_historical_ticket(self, guild_id: int, discord_user_id: int, mc_name: str,
                                 mc_uuid: Optional[str], created_at: int,
                                 note: Optional[str] = None,
                                 kind: str = KIND_RESCUE) -> int:
        """A closed ticket that happened before this bot existed.

        Written with its ORIGINAL timestamps rather than now(): the cooldown is computed from
        dates, so importing a year of history with today's date would put the entire community
        on cooldown the day the bot goes live.
        """
        cur = self._db.execute(
            "INSERT INTO tickets(guild_id, discord_user_id, mc_name, mc_uuid, status, note, "
            "request_type, created_at, closed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(guild_id), int(discord_user_id), mc_name, mc_uuid, STATUS_CANCELLED,
             note, kind, int(created_at), int(created_at)))
        return int(cur.lastrowid)

    def record_historical_kit(self, guild_id: int, ticket_id: Optional[int],
                              discord_user_id: int, mc_name: str, mc_uuid: Optional[str],
                              created_at: int, kind: str = KIND_RESCUE) -> int:
        """The same, for the grant. Delivered at its original date, so it reads as finished."""
        cur = self._db.execute(
            "INSERT INTO kits(guild_id, ticket_id, discord_user_id, mc_uuid, mc_name, kind, "
            "created_at, delivered_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(guild_id), ticket_id, int(discord_user_id), mc_uuid, mc_name, kind,
             int(created_at), int(created_at)))
        return int(cur.lastrowid)

    def claims_for_ticket(self, ticket_id: int,
                          guild_id: Optional[int] = None) -> List[sqlite3.Row]:
        """The same, across every dispatch on a ticket -- what the transcript reports."""
        if guild_id is None:
            return list(self._db.execute(
                "SELECT c.* FROM kit_claims c JOIN kits k ON k.id = c.kit_id "
                "WHERE k.ticket_id=? ORDER BY c.id", (int(ticket_id),)))
        return list(self._db.execute(
            "SELECT c.* FROM kit_claims c JOIN kits k ON k.id = c.kit_id "
            "WHERE k.ticket_id=? AND k.guild_id=? ORDER BY c.id",
            (int(ticket_id), int(guild_id))))

    def get_kit(self, kit_id: int,
                guild_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        """Pass `guild_id` from anything a user can name a dispatch number to -- see
        `get_ticket` for why."""
        if guild_id is None:
            return self._db.execute("SELECT * FROM kits WHERE id=?",
                                    (int(kit_id),)).fetchone()
        return self._db.execute("SELECT * FROM kits WHERE id=? AND guild_id=?",
                                (int(kit_id), int(guild_id))).fetchone()

    # ---------------------------------------------------------------- cooldown

    def last_kit(self, guild_id: int, discord_user_id: Optional[int] = None,
                 mc_uuid: Optional[str] = None,
                 kind: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Most recent kit in THIS guild matching either identifier.

        Either identifier alone is easy to sidestep -- a second Discord account with the same
        MC account, or the reverse -- so both are checked. Scoped per guild: each server's kit
        history is its own, so a kit from one does not start a cooldown in another.

        `kind` narrows to one sort of grant, which is what keeps the two cooldowns independent.
        Left as None it means "any grant", which is what this method meant before there were
        two kinds -- so a caller that has no opinion still gets the old behaviour.
        """
        clauses, args = [], [int(guild_id)]
        if discord_user_id is not None:
            clauses.append("discord_user_id=?")
            args.append(int(discord_user_id))
        if mc_uuid:
            clauses.append("mc_uuid=?")
            args.append(mc_uuid)
        if not clauses:
            return None
        sql = "SELECT * FROM kits WHERE guild_id=? AND (%s)" % (" OR ".join(clauses),)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        return self._db.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()

    def cooldown(self, guild_id: int, cooldown_days: int,
                 discord_user_id: Optional[int] = None,
                 mc_uuid: Optional[str] = None, now: Optional[int] = None,
                 kind: Optional[str] = None) -> Dict[str, Any]:
        """``{'blocked': bool, 'days_left': int, 'last_at': int|None, 'matched': str|None}``

        Pass `kind` to ask about one cooldown. A rescue kit and a funding grant run their own
        clocks, so asking without a kind answers a question nobody has: "have they had
        anything at all recently", which would block a rescue because a build got funded.
        """
        row = self.last_kit(guild_id, discord_user_id, mc_uuid, kind)
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

    def kit_history(self, guild_id: int, discord_user_id: Optional[int] = None,
                    mc_uuid: Optional[str] = None, limit: int = 10,
                    kind: Optional[str] = None) -> List[sqlite3.Row]:
        """Past grants, newest first. `kind` None means both sorts -- the reviewer card wants
        the whole history even when only one cooldown applies."""
        clauses, args = [], [int(guild_id)]
        if discord_user_id is not None:
            clauses.append("discord_user_id=?")
            args.append(int(discord_user_id))
        if mc_uuid:
            clauses.append("mc_uuid=?")
            args.append(mc_uuid)
        if not clauses:
            return []
        sql = "SELECT * FROM kits WHERE guild_id=? AND (%s)" % (" OR ".join(clauses),)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        args.append(int(limit))
        return list(self._db.execute(
            sql + " ORDER BY created_at DESC LIMIT ?", args))

    # Statuses that start the per-identity request clock. Cancelled is absent on purpose: the bot
    # cancels a ticket itself when it cannot post the reviewer card, precisely so the applicant
    # can retry at once, and counting that would lock somebody out for half a year over our bug.
    # Open counts -- an abandoned open ticket is still a request that was made.
    REQUEST_CLOCK_STATUSES = (STATUS_OPEN, STATUS_APPROVED, STATUS_DECLINED)

    def last_request(self, guild_id: int, discord_user_id: int,
                     exclude_ticket_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        """This Discord identity's most recent ticket in this guild, whatever came of it.

        Keyed on the Discord id alone, unlike `last_kit`, which also matches the MC account.
        That is deliberate and it is the difference between the two clocks: this one asks "has
        this person asked recently", and letting a different MC name reset it would make the
        answer "no" for anyone who typed a new username.
        """
        marks = ",".join("?" * len(self.REQUEST_CLOCK_STATUSES))
        sql = ("SELECT * FROM tickets WHERE guild_id=? AND discord_user_id=? "
               "AND status IN (%s)" % marks)
        args: List[Any] = [int(guild_id), int(discord_user_id)]
        args.extend(self.REQUEST_CLOCK_STATUSES)
        if exclude_ticket_id is not None:
            sql += " AND id<>?"
            args.append(int(exclude_ticket_id))
        return self._db.execute(
            sql + " ORDER BY created_at DESC, id DESC LIMIT 1", args).fetchone()

    def request_cooldown(self, guild_id: int, cooldown_days: int, discord_user_id: int,
                         now: Optional[int] = None,
                         exclude_ticket_id: Optional[int] = None) -> Dict[str, Any]:
        """``{'blocked', 'days_left', 'last_at', 'last_ticket_id', 'last_status'}``

        `exclude_ticket_id` exists because the card for ticket N must not report that ticket N
        blocks itself: `gather` runs after the row is written, so the applicant's own request is
        the most recent one by definition.
        """
        empty = {"blocked": False, "days_left": 0, "last_at": None,
                 "last_ticket_id": None, "last_status": None}
        # Coerced rather than trusted: a per-guild override arrives from the config table as
        # whatever was stored there, and `"180" <= 0` is a TypeError rather than a comparison.
        try:
            cooldown_days = int(cooldown_days)
        except (TypeError, ValueError):
            return empty
        if cooldown_days <= 0:
            return empty
        row = self.last_request(guild_id, discord_user_id, exclude_ticket_id)
        if row is None:
            return empty
        now = _now() if now is None else int(now)
        elapsed = now - int(row["created_at"])
        window = int(cooldown_days) * 86400
        out = {"blocked": elapsed < window,
               # Ceiling, so "1 day left" never means "come back in 40 minutes".
               "days_left": int(-(-(window - elapsed) // 86400)) if elapsed < window else 0,
               "last_at": int(row["created_at"]),
               "last_ticket_id": int(row["id"]),
               "last_status": str(row["status"])}
        return out

    # --------------------------------------------------------------- purging
    #
    # A finished ticket's Discord footprint is deleted after a grace period: the applicant's
    # thread and the staff queue post both go, and the transcript in the archive channel becomes
    # the only record left in Discord. That is exactly why `mark_transcribed` exists and why
    # `tickets_to_purge` will not return a ticket without it -- deleting both sides of a ticket
    # whose transcript never posted would leave nothing at all.

    def mark_transcribed(self, ticket_id: int, when: Optional[int] = None) -> None:
        """Record that a transcript reached the archive channel. Only ever called on success."""
        self._db.execute("UPDATE tickets SET transcript_at=? WHERE id=? AND transcript_at IS NULL",
                         (_now() if when is None else int(when), int(ticket_id)))

    def tickets_to_purge(self, grace_seconds: int, now: Optional[int] = None,
                         limit: int = 25) -> List[sqlite3.Row]:
        """Closed tickets whose grace period has run out and which still have something to
        delete. Never returns an open ticket, and never one without a transcript.

        `limit` keeps one sweep bounded: each row costs up to two Discord deletes, and a first
        run against a long-neglected guild should not turn into a few hundred API calls in a
        burst. Whatever is left is picked up by the next sweep.
        """
        if grace_seconds < 0:
            return []
        now = _now() if now is None else int(now)
        return list(self._db.execute(
            "SELECT * FROM tickets "
            " WHERE status<>? "
            "   AND closed_at IS NOT NULL AND closed_at<=? "
            "   AND transcript_at IS NOT NULL "
            "   AND purged_at IS NULL "
            "   AND (thread_id IS NOT NULL OR queue_thread_id IS NOT NULL) "
            " ORDER BY closed_at LIMIT ?",
            (STATUS_OPEN, now - int(grace_seconds), int(limit))))

    def tickets_needing_transcript(self, grace_seconds: int, now: Optional[int] = None,
                                   limit: int = 10) -> List[sqlite3.Row]:
        """Closed, past the grace period, still has threads, and has NO transcript on record.

        Every ticket closed before `transcript_at` existed looks like this. Their transcripts
        very probably did post -- the code has always tried -- but "probably" is not a basis for
        deleting both surviving copies, so the sweeper writes a fresh one and purges them on the
        next pass. That makes the invariant true rather than assumed, at the cost of one
        duplicate in the archive per historical ticket, once.
        """
        if grace_seconds < 0:
            return []
        now = _now() if now is None else int(now)
        return list(self._db.execute(
            "SELECT * FROM tickets "
            " WHERE status<>? "
            "   AND closed_at IS NOT NULL AND closed_at<=? "
            "   AND transcript_at IS NULL "
            "   AND purged_at IS NULL "
            "   AND (thread_id IS NOT NULL OR queue_thread_id IS NOT NULL) "
            " ORDER BY closed_at LIMIT ?",
            (STATUS_OPEN, now - int(grace_seconds), int(limit))))

    def mark_purged(self, ticket_id: int, when: Optional[int] = None) -> None:
        """Forget the thread ids and stamp when. The ids are cleared so a failed delete is
        retried and a successful one is never retried, and `purged_at` is kept so that "this
        vanished from Discord on purpose, on this date" stays answerable."""
        self._db.execute(
            "UPDATE tickets SET thread_id=NULL, queue_thread_id=NULL, purged_at=? WHERE id=?",
            (_now() if when is None else int(when), int(ticket_id)))

    def other_requesters(self, guild_id: int, mc_uuid: Optional[str] = None,
                         mc_name: Optional[str] = None,
                         exclude_user_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Other Discord identities that have opened a ticket for THIS Minecraft account.

        The kit-farm shape, and the opposite direction from `linked_accounts`: that one asks
        which MC names sit behind one Discord account (an alt), this one asks which Discord
        accounts sit in front of one MC account (a farm). A second Discord identity asking for an
        account somebody was already helped on is the pattern worth naming.

        Matched on uuid when there is one and on name only as a fallback, because a rename would
        otherwise split one MC account's history -- and a name match alone is weak enough that
        the card says which of the two it was.

        A uuid match ALSO picks up rows whose uuid is null and whose name matches. Those are
        tickets opened while the identity lookup was down, and without this they would be
        unreachable forever: a farm only has to make one request during an outage to get a row
        that no later uuid search can see. Two accounts can only share a name across a rename,
        so the false-positive risk is much smaller than the hole it closes.

        Cancelled tickets are included here, unlike the request clock: for evidence, an attempt
        that never reached a reviewer still shows somebody tried.
        """
        if not mc_uuid and not mc_name:
            return []
        if mc_uuid:
            where = "(mc_uuid=? OR (mc_uuid IS NULL AND LOWER(mc_name)=?))"
            args = [mc_uuid, str(mc_name or "").lower()]
        else:
            where, args = "(mc_uuid IS NULL AND LOWER(mc_name)=?)", [str(mc_name).lower()]
        sql = ("SELECT discord_user_id, mc_name, mc_uuid, COUNT(*) AS tickets, "
               "MAX(created_at) AS newest, "
               "SUM(CASE WHEN status=? THEN 1 ELSE 0 END) AS approved, "
               "SUM(CASE WHEN status=? THEN 1 ELSE 0 END) AS declined "
               "FROM tickets WHERE guild_id=? AND %s" % where)
        params: List[Any] = [STATUS_APPROVED, STATUS_DECLINED, int(guild_id)]
        params.extend(args)
        if exclude_user_id is not None:
            sql += " AND discord_user_id<>?"
            params.append(int(exclude_user_id))
        return list(self._db.execute(
            sql + " GROUP BY discord_user_id ORDER BY newest DESC LIMIT 25", params))

    def linked_accounts(self, guild_id: int, discord_user_id: Optional[int] = None,
                        mc_uuid: Optional[str] = None) -> List[sqlite3.Row]:
        """Other identities that have shared a ticket with these -- the ledger fan-out.

        Evidence for a reviewer, not a verdict: sharing a Discord account with another MC
        name is what an alt looks like *and* what a sibling, a rename, or a borrowed account
        looks like. Scoped per guild, so one server's reviewers never see who another server
        has helped.
        """
        if discord_user_id is None and not mc_uuid:
            return []
        return list(self._db.execute(
            "SELECT DISTINCT discord_user_id, mc_uuid, mc_name FROM tickets "
            "WHERE guild_id=? AND (discord_user_id=? OR mc_uuid=?) ORDER BY id DESC LIMIT 25",
            (int(guild_id),
             int(discord_user_id) if discord_user_id is not None else -1, mc_uuid or "")))

    # ------------------------------------------------------------------- flags

    def set_flag(self, guild_id: int, kind: str, set_by: int,
                 mc_uuid: Optional[str] = None, mc_name: Optional[str] = None,
                 note: Optional[str] = None) -> int:
        if not mc_uuid and not mc_name:
            raise ValueError("a flag needs a uuid or a name")
        cur = self._db.execute(
            "INSERT INTO flags(guild_id, kind, mc_uuid, mc_name, note, set_by, set_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(guild_id), kind, mc_uuid, mc_name, note, int(set_by), _now()))
        return int(cur.lastrowid)

    def clear_flag(self, guild_id: int, flag_id: int) -> bool:
        """Guild-scoped: a flag id is user-supplied, so one server must not clear another's."""
        cur = self._db.execute(
            "UPDATE flags SET cleared_at=? WHERE id=? AND guild_id=? AND cleared_at IS NULL",
            (_now(), int(flag_id), int(guild_id)))
        return cur.rowcount > 0

    def flags_for(self, guild_id: int, mc_uuid: Optional[str] = None,
                  mc_name: Optional[str] = None) -> List[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT * FROM flags WHERE guild_id=? AND cleared_at IS NULL AND "
            "(mc_uuid IS NOT NULL AND mc_uuid=? OR mc_name IS NOT NULL AND "
            "LOWER(mc_name)=LOWER(?)) ORDER BY id DESC",
            (int(guild_id), mc_uuid or "", mc_name or "")))

    # --------------------------------------------------------- instrumentation

    def shown_chats_for_guild(self, guild_id: int, ticket_id: int) -> List[sqlite3.Row]:
        """The chat lines a reviewer was shown, scoped to the guild owning that ticket.

        This is the read side the in-Discord chat pager runs on, and the reason the pager can
        exist at all: the lines were persisted when the card was built, so paging through them
        needs no second call to api.2b2t.vc and survives a restart. What is stored is what was
        shown, already coordinate-redacted.

        Scoped, unlike the plain `shown_chats` below, and the distinction is the point. That
        one is safe for an internal caller that already holds a ticket row it trusts -- the
        transcript builder. This one is for anything reached by a ticket number a *user* can
        supply, which includes a button whose custom_id carries one. shown_chats has no
        guild_id of its own -- deliberately, see `_migrate` -- so the scope comes from a join
        onto its parent. Row ids are globally unique, so filtering on ticket_id alone would
        hand one server's reviewers another server's chat history for the price of guessing a
        number. Same argument as `get_ticket`.
        """
        return list(self._db.execute(
            "SELECT s.position, s.chat_ts, s.chat, s.flagged FROM shown_chats s "
            "JOIN tickets t ON t.id = s.ticket_id "
            "WHERE s.ticket_id=? AND t.guild_id=? ORDER BY s.position",
            (int(ticket_id), int(guild_id))))

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

    def counts(self, guild_id: Optional[int] = None) -> Dict[str, int]:
        """Row counts. With `guild_id`, only that guild's -- decisions and shown_chats are
        reached through their ticket, since they carry no guild column of their own."""
        out = {}
        if guild_id is None:
            for table in ("tickets", "decisions", "kits", "flags", "shown_chats"):
                out[table] = int(self._db.execute(
                    "SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"])
            out["flagged_chats"] = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM shown_chats WHERE flagged=1").fetchone()["n"])
            return out

        g = (int(guild_id),)
        for table in ("tickets", "kits", "flags"):
            out[table] = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE guild_id=?" % table, g).fetchone()["n"])
        for table in ("decisions", "shown_chats"):
            out[table] = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE ticket_id IN "
                "(SELECT id FROM tickets WHERE guild_id=?)" % table, g).fetchone()["n"])
        out["flagged_chats"] = int(self._db.execute(
            "SELECT COUNT(*) AS n FROM shown_chats WHERE flagged=1 AND ticket_id IN "
            "(SELECT id FROM tickets WHERE guild_id=?)", g).fetchone()["n"])
        return out


def open_store(path: str) -> Store:
    return Store(path)
