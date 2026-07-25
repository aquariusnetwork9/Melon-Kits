"""Canonical record schema, dedupe key, and per-source normalizers.

Implements SPEC §2 (canonical 12-key record), §2.1 (per-source field-name mapping),
§4.2 (dedupe key), §12.2/§12.3 (``precision`` per source) and §12.4 (key granularity
and the null-UUID fallback).

Two rules in here are load-bearing and must not be "simplified":

1. **Two separate normalizers.** SSE ``/feed/chats`` names the UUID field ``playerUuid``;
   REST ``/chats/window`` names it ``uuid`` (SPEC §2.1). There is deliberately no shared
   accessor with a fallback chain: a fallback chain hides the day the API renames a field
   and silently produces a corpus of null UUIDs. Each normalizer reads exactly the names
   its own source uses and raises :class:`RecordError` when one is absent.
2. **``component`` is stored verbatim.** It is a JSON string containing JSON (SPEC §2,
   §12.10). It is never decoded, validated, or re-encoded here, and it is ``None`` for
   every non-SSE source.

Privacy (SPEC §11.4): no exception raised by this module may contain a chat body, a
component body, or any other record content. Every message in here is built from
metadata only — field names, Python type names, and character counts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional, Tuple

import tsutil

#: The 12 canonical keys, in SPEC §2 order. Authoritative — every emitted line carries
#: all of them, always, in exactly this order.
KEYS = (
    "ts",
    "ts_us",
    "player_uuid",
    "player_name",
    "chat",
    "component",
    "src",
    "precision",
    "row_id",
    "ingest_ts",
    "seq",
    "batch",
)  # type: Tuple[str, ...]

SRC_SSE = "sse"
SRC_BACKFILL = "rest-backfill"
SRC_LOG = "proxy-log"

#: SPEC §12.2 — ``/chats/window`` returns genuine microseconds, so backfill rows are
#: ``"us"`` exactly like SSE rows. SPEC §12.3 — the proxy log is ``HH:mm:ss``, whole
#: seconds, so log rows are ``"s"``.
PRECISION_US = "us"
PRECISION_S = "s"

#: SPEC §4.2 — proxy-log row_ids live in their own namespace and can therefore never
#: collide with an SSE/backfill row_id.
LOG_ROW_ID_PREFIX = "L"

_US_PER_S = 1000000

_KEYSET = frozenset(KEYS)

_UNIT_SEP = b"\x1f"

_ROW_ID_HEX = 32

#: What a lone surrogate is replaced by. U+FFFD is what ``errors="replace"``
#: already puts in the stream for malformed UTF-8 bytes (SPEC §6.2), so text that
#: cannot be encoded is normalised the same way rather than being dropped.
_REPLACEMENT = "�"


class RecordError(ValueError):
    """A source object could not be normalized into a canonical record (SPEC §2.1).

    Raised on a missing or wrongly-typed source field. The message contains metadata
    only — never a record body (SPEC §11.4).
    """


# --------------------------------------------------------------------------- #
# internal helpers — metadata-only diagnostics
# --------------------------------------------------------------------------- #


def _shape(value: Any) -> str:
    """Describe a value for an exception message without revealing it (SPEC §11.4)."""
    name = type(value).__name__
    if isinstance(value, str):
        return "%s(len=%d)" % (name, len(value))
    if isinstance(value, (bytes, bytearray)):
        return "%s(len=%d)" % (name, len(value))
    return name


def _encodable(value: str) -> str:
    """Return `value` with any unencodable code point replaced by U+FFFD.

    ``json.loads`` happily turns a ``"\\ud800"`` escape into a Python string holding
    a **lone surrogate**, which is not invalid UTF-8 *on the wire* and therefore
    survives the reader's ``errors="replace"`` decode untouched. Such a string then
    raises ``UnicodeEncodeError`` from ``str.encode("utf-8")`` — once inside
    :func:`row_id` and again inside :func:`encode`. Neither call site can do anything
    useful with that: the row is either silently dropped (SSE, where the caller
    catches broadly and counts a parse error) or it escapes ``Writer.put_record``
    and kills the producing thread.

    A corpus is worth more than a crash, so the row is kept with the offending code
    point normalised, exactly as a malformed UTF-8 byte would already have been.
    The fast path is one ``encode`` and no allocation for the overwhelmingly common
    case of clean text.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(
            _REPLACEMENT if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value
        )
    return value


def _require(obj: Dict[str, Any], field: str, source: str) -> Any:
    """Fetch ``field`` from a source object or raise (SPEC §2.1).

    A *missing* key is always an error: it is how a server-side field rename announces
    itself. A key that is present with a ``null`` value is a data fact and is handled by
    the caller.
    """
    if not isinstance(obj, dict):
        raise RecordError(
            "%s payload is not a JSON object: %s" % (source, _shape(obj))
        )
    if field not in obj:
        raise RecordError("%s payload is missing required field %r" % (source, field))
    return obj[field]


def _req_str(obj: Dict[str, Any], field: str, source: str) -> str:
    """Fetch a required non-null string field, metadata-only errors (SPEC §2.1)."""
    value = _require(obj, field, source)
    if not isinstance(value, str):
        raise RecordError(
            "%s field %r must be a string, got %s" % (source, field, _shape(value))
        )
    return _encodable(value)


def _opt_uuid(obj: Dict[str, Any], field: str, source: str) -> Optional[str]:
    """Fetch the required-but-nullable UUID field (SPEC §2, §12.4).

    The key must be present; its value may be ``null``. Returns a lowercased string or
    ``None``. An empty/whitespace string is normalized to ``None`` so that the stored
    ``player_uuid`` and the ``row_id`` identity always agree (SPEC §4.2). No shape
    validation happens here: the record is stored as received and any shape auditing is
    an offline concern (SPEC §12.10 spirit).

    Surrounding whitespace is **stripped, not merely tested for**: if one endpoint ever
    delivers ``" <uuid> "`` and the other the bare form, an un-stripped value would hash
    to a different ``row_id`` and the SSE row and its ``/chats/window`` twin would both
    land on disk (SPEC §4.2 — the two must collide).
    """
    value = _require(obj, field, source)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RecordError(
            "%s field %r must be a string or null, got %s"
            % (source, field, _shape(value))
        )
    value = value.strip()
    if not value:
        return None
    return _encodable(value.lower())


def _req_ts_us(obj: Dict[str, Any], field: str, source: str) -> int:
    """Parse the source timestamp into ts_us via tsutil (SPEC §3.1).

    The offending value is never interpolated into the error: a renamed field could put
    arbitrary content under this key (SPEC §11.4).
    """
    raw = _require(obj, field, source)
    if not isinstance(raw, str):
        raise RecordError(
            "%s field %r must be a timestamp string, got %s"
            % (source, field, _shape(raw))
        )
    try:
        return tsutil.parse_ts(raw)
    except tsutil.TsParseError:
        # `from None`: tsutil's own message may interpolate the offending value, and a
        # chained traceback would carry it into the log (SPEC §11.4).
        raise RecordError(
            "%s field %r is not a parseable timestamp (%s)"
            % (source, field, _shape(raw))
        ) from None


def _req_int(value: Any, name: str) -> int:
    """Validate a caller-supplied integer (``seq``, ``ingest_us``, ``ts_us``)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError("%s must be an int, got %s" % (name, _shape(value)))
    return value


def _opt_batch(value: Any) -> Optional[str]:
    """Validate the nullable provenance field (SPEC §2 key 12)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise RecordError("batch must be a string or null, got %s" % (_shape(value),))
    return value


def _build(
    ts_us: int,
    player_uuid: Optional[str],
    player_name: str,
    chat: str,
    component: Optional[str],
    src: str,
    precision: str,
    rid: str,
    seq: int,
    batch: Optional[str],
    ingest_us: int,
) -> Dict[str, Any]:
    """Assemble the canonical 12-key record in SPEC §2 order.

    ``ts`` is always rendered from ``ts_us`` so the two can never disagree (SPEC §2).
    """
    return {
        "ts": tsutil.fmt_ts(ts_us),
        "ts_us": ts_us,
        "player_uuid": player_uuid,
        "player_name": player_name,
        "chat": chat,
        "component": component,
        "src": src,
        "precision": precision,
        "row_id": rid,
        "ingest_ts": tsutil.fmt_ts(ingest_us),
        "seq": seq,
        "batch": batch,
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def row_id(ts_us: int, uuid: Optional[str], name: str, chat: str) -> str:
    """SPEC §4.2 — the dedupe key: 32 lowercase hex chars.

    ``sha256`` over ``b'\\x1f'``-joined ``(str(ts_us), ident, chat)``, truncated to 32 hex
    characters, where ``ident`` is ``uuid.lower()`` when a UUID is present and
    ``'name:' + name`` when it is not (SPEC §12.4 — ``player_name`` is NOT NULL, so the
    fallback is always available).

    Keying on the **integer** ``ts_us`` rather than the raw timestamp string is
    mandatory: the same instant arrives as ``.11`` from one endpoint and ``.110000``
    from another, and only the integer makes those two collide (SPEC §4.2).
    """
    ts_us = _req_int(ts_us, "ts_us")
    if uuid is not None and not isinstance(uuid, str):
        raise RecordError("uuid must be a string or null, got %s" % (_shape(uuid),))
    if not isinstance(name, str):
        raise RecordError("name must be a string, got %s" % (_shape(name),))
    if not isinstance(chat, str):
        raise RecordError("chat must be a string, got %s" % (_shape(chat),))
    # _encodable() keeps this function total: a lone surrogate (which json.loads can
    # produce and which errors="replace" does not remove) would otherwise raise
    # UnicodeEncodeError here instead of RecordError, and the value hashed must be
    # the value that ends up on disk.
    ident = _encodable(uuid.lower() if uuid else "name:" + name)
    blob = _UNIT_SEP.join(
        (str(ts_us).encode("ascii"), ident.encode("utf-8"),
         _encodable(chat).encode("utf-8"))
    )
    return hashlib.sha256(blob).hexdigest()[:_ROW_ID_HEX]


def from_sse(obj: dict, *, seq: int, batch: str, ingest_us: int) -> dict:
    """Normalize one SSE ``/feed/chats`` event into a canonical record (SPEC §2, §2.1).

    Reads exactly the SSE field names: ``time``, ``chat``, ``playerName``,
    ``playerUuid``, ``component``. ``playerUuid`` is the SSE spelling and this normalizer
    accepts no other spelling — see the module docstring. ``component`` is carried through
    byte-for-byte as received (SPEC §2 key 6, §12.10). ``precision`` is ``"us"``: the feed
    emits nanoseconds, floor-truncated to the DB's microsecond resolution (SPEC §3.1).
    """
    seq = _req_int(seq, "seq")
    ingest_us = _req_int(ingest_us, "ingest_us")
    batch_v = _opt_batch(batch)

    ts_us = _req_ts_us(obj, "time", SRC_SSE)
    chat = _req_str(obj, "chat", SRC_SSE)
    name = _req_str(obj, "playerName", SRC_SSE)
    uuid = _opt_uuid(obj, "playerUuid", SRC_SSE)

    component = _require(obj, "component", SRC_SSE)
    if component is not None and not isinstance(component, str):
        raise RecordError(
            "%s field 'component' must be a string or null, got %s"
            % (SRC_SSE, _shape(component))
        )
    if component is not None:
        # Verbatim (SPEC §12.10) means never decoded, validated or re-encoded — but a
        # code point that cannot be UTF-8 encoded at all would make the whole record
        # unwritable, so it is normalised like a malformed byte and nothing else is
        # touched.
        component = _encodable(component)

    return _build(
        ts_us,
        uuid,
        name,
        chat,
        component,
        SRC_SSE,
        PRECISION_US,
        row_id(ts_us, uuid, name, chat),
        seq,
        batch_v,
        ingest_us,
    )


def from_window(obj: dict, *, seq: int, batch: str, ingest_us: int) -> dict:
    """Normalize one REST ``/chats/window`` row into a canonical record (SPEC §2, §2.1).

    Reads exactly the window field names: ``time``, ``chat``, ``playerName``, ``uuid``.
    ``uuid`` is the window spelling and this normalizer accepts no other spelling — a
    fallback onto SSE's ``playerUuid`` would hide a rename and yield null UUIDs.
    ``component`` is absent from every REST response and is therefore ``None`` (SPEC §2.1);
    it is never read from the payload even if a future response were to carry it.
    ``precision`` is ``"us"`` — the window endpoint returns genuine microseconds, which
    SPEC §12.2 rules on explicitly.
    """
    seq = _req_int(seq, "seq")
    ingest_us = _req_int(ingest_us, "ingest_us")
    batch_v = _opt_batch(batch)

    ts_us = _req_ts_us(obj, "time", SRC_BACKFILL)
    chat = _req_str(obj, "chat", SRC_BACKFILL)
    name = _req_str(obj, "playerName", SRC_BACKFILL)
    uuid = _opt_uuid(obj, "uuid", SRC_BACKFILL)

    return _build(
        ts_us,
        uuid,
        name,
        chat,
        None,
        SRC_BACKFILL,
        PRECISION_US,
        row_id(ts_us, uuid, name, chat),
        seq,
        batch_v,
        ingest_us,
    )


def from_logline(
    ts_us: int, name: str, chat: str, *, seq: int, batch: str, ingest_us: int
) -> dict:
    """Normalize one parsed proxy ``latest.log`` chat line (SPEC §8.2, §8.5, §12.3).

    The log line carries no UUID and no component, and its timestamp is whole seconds:
    ``player_uuid=None``, ``component=None``, ``precision="s"``, and ``ts_us`` floored to
    the second so ``ts`` and ``ts_us`` cannot claim resolution the source does not have.

    The ``row_id`` is ``"L"`` + the SPEC §4.2 hash, giving log rows their own dedupe
    namespace. It can never equal an SSE row_id (32 hex chars never start with ``L``), which
    is what lets log rows share the file without ever being mistaken for authoritative
    rows (SPEC §8.5).
    """
    ts_us = _req_int(ts_us, "ts_us")
    seq = _req_int(seq, "seq")
    ingest_us = _req_int(ingest_us, "ingest_us")
    batch_v = _opt_batch(batch)
    if not isinstance(name, str):
        raise RecordError("name must be a string, got %s" % (_shape(name),))
    if not isinstance(chat, str):
        raise RecordError("chat must be a string, got %s" % (_shape(chat),))
    name = _encodable(name)
    chat = _encodable(chat)

    ts_us = (ts_us // _US_PER_S) * _US_PER_S

    return _build(
        ts_us,
        None,
        name,
        chat,
        None,
        SRC_LOG,
        PRECISION_S,
        LOG_ROW_ID_PREFIX + row_id(ts_us, None, name, chat),
        seq,
        batch_v,
        ingest_us,
    )


def encode(rec: dict) -> bytes:
    """Serialize a canonical record to exactly one JSONL line (SPEC §2, §5.5).

    ``json.dumps`` with ``separators=(',',':')``, ``ensure_ascii=False``,
    ``sort_keys=False`` and the keys re-emitted in :data:`KEYS` order, UTF-8 encoded, with
    exactly one trailing ``b'\\n'`` and no other newline (JSON escapes any newline inside a
    string value, so a chat body can never break the one-record-per-line invariant).

    A missing or unexpected key raises :class:`RecordError`; only key *names* appear in
    the message (SPEC §11.4).
    """
    if not isinstance(rec, dict):
        raise RecordError("record must be a dict, got %s" % (_shape(rec),))
    missing = [k for k in KEYS if k not in rec]
    if missing:
        raise RecordError("record is missing keys: %s" % (",".join(missing),))
    extra = sorted(k for k in rec if k not in _KEYSET)
    if extra:
        raise RecordError("record has unexpected keys: %s" % (",".join(map(str, extra)),))
    ordered = {}  # type: Dict[str, Any]
    for k in KEYS:
        ordered[k] = rec[k]
    line = json.dumps(
        ordered, separators=(",", ":"), ensure_ascii=False, sort_keys=False
    )
    try:
        return line.encode("utf-8") + b"\n"
    except UnicodeEncodeError:
        # Only reachable for a hand-built record: every normalizer here runs its text
        # through _encodable() first. Surfaced as RecordError (metadata only, no
        # offset and no offending text) so a producer sees the documented exception
        # type rather than an escaping UnicodeEncodeError that no caller catches.
        raise RecordError(
            "record contains text that is not UTF-8 encodable (chars=%d)" % (len(line),)
        ) from None
