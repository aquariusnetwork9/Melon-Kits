#!/usr/bin/env python3
"""Corpus shape report -- the operator's "is collection actually working?" check.

Streams every ``chat-*.jsonl*`` file in ``data_dir`` and reports rows per UTC
day, rows by ``src``, distinct players, the event-time range, how many rows carry
a null ``player_uuid``, and the mean/median chat length. Supports SPEC §11.3's
manual-check role: ``--health`` tells you the process is alive, this tells you
the process is producing data of the right shape.

Everything is streamed and every aggregate is bounded:

* rows per day is a dict keyed by date -- one entry per day of corpus.
* chat lengths go into a **length histogram**, not a list, so the exact median
  costs one dict entry per distinct length instead of 8 bytes per row.
* distinct players are counted through 64-bit fingerprints, so the sets cost
  ints rather than UUID/name strings. Exact up to a 64-bit collision, which at
  a million distinct players is a ~3e-8 chance of undercounting by one.

Privacy (SPEC §11.4): this tool reads every chat body in the corpus and prints
none of them. Chat text appears only as a *length*, and player identity only as
a *count* -- there is deliberately no "top talkers" or "sample rows" mode,
because 2b2t chat routinely contains coordinates and this output is the kind of
thing that gets pasted into a chat window.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sys
import zlib
from hashlib import blake2b
from typing import Any, Dict, IO, Iterator, List, Optional, Set, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402  (path bootstrap must run first)
import tsutil  # noqa: E402

#: SPEC §5.1/§5.2 corpus file names; ``.gz.tmp`` work in progress is excluded.
_FILE_RE = re.compile(r"^chat-\d{4}-\d{2}-\d{2}(?:\.\d+)?\.jsonl(?:\.gz)?$")

_US_PER_S = 1000000


def _warn(msg: str) -> None:
    """Metadata-only diagnostic on stderr (SPEC §11.4)."""
    sys.stderr.write("corpus_stats: %s\n" % (msg,))


def corpus_files(data_dir: str) -> List[str]:
    """Every corpus data file in `data_dir`, oldest first (SPEC §5.1, §5.2)."""
    found = []  # type: List[str]
    for path in glob.glob(os.path.join(data_dir, "chat-*.jsonl*")):
        if _FILE_RE.match(os.path.basename(path)):
            found.append(path)
    found.sort(key=lambda p: os.path.basename(p))
    return found


def open_corpus_file(path: str) -> IO[bytes]:
    """Open one corpus file for streaming, picking the reader by suffix."""
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def iter_rows(paths: List[str], stats: Dict[str, int]) -> Iterator[Dict[str, Any]]:
    """Stream decoded records from `paths` (SPEC §5.5).

    Never holds more than one line in memory. Undecodable lines are counted,
    never echoed (SPEC §11.4). A truncated final gzip member -- the normal shape
    of a corpus file interrupted mid-compression -- ends that file and continues.
    """
    for path in paths:
        stats["files"] += 1
        try:
            handle = open_corpus_file(path)
        except OSError as exc:
            stats["read_errors"] += 1
            _warn("cannot open %s (%s)" % (os.path.basename(path), type(exc).__name__))
            continue
        try:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    stats["malformed"] += 1
                    continue
                if not isinstance(obj, dict):
                    stats["malformed"] += 1
                    continue
                yield obj
        except (OSError, EOFError, zlib.error) as exc:
            stats["read_errors"] += 1
            _warn(
                "truncated or unreadable file %s (%s); continuing"
                % (os.path.basename(path), type(exc).__name__)
            )
        finally:
            try:
                handle.close()
            except OSError:
                pass


def _fingerprint(value: str) -> int:
    """64-bit fingerprint of an identity string, for bounded distinct counts.

    Storing an int instead of the string keeps the distinct-player sets small on
    a multi-gigabyte corpus. It also means no player name or UUID is retained
    anywhere in this process's memory longer than one row (SPEC §11.4 in spirit).
    """
    return int.from_bytes(blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")


class Accumulator:
    """Bounded streaming aggregates over the corpus. SPEC §11.3 (manual check).

    All state here is either O(days), O(sources), O(distinct chat lengths) or a
    set of 8-byte fingerprints -- nothing grows with row count except the
    fingerprint sets, which grow with *distinct players*.
    """

    def __init__(self) -> None:
        self.rows = 0
        self.by_day = {}  # type: Dict[str, int]
        self.by_src = {}  # type: Dict[str, int]
        self.by_precision = {}  # type: Dict[str, int]
        self.uuid_fps = set()  # type: Set[int]
        self.name_fps = set()  # type: Set[int]
        self.null_uuid = 0
        self.with_component = 0
        self.min_us = None  # type: Optional[int]
        self.max_us = None  # type: Optional[int]
        self.min_ingest_us = None  # type: Optional[int]
        self.max_ingest_us = None  # type: Optional[int]
        self.len_hist = {}  # type: Dict[int, int]
        self.len_total = 0
        self.len_max = 0
        self.bad_rows = 0

    def add(self, obj: Dict[str, Any]) -> bool:
        """Fold one record in. False when the row is unusable (counted as bad)."""
        ts_us = obj.get("ts_us")
        chat = obj.get("chat")
        name = obj.get("player_name")
        uuid = obj.get("player_uuid")
        src = obj.get("src")
        if isinstance(ts_us, bool) or not isinstance(ts_us, int):
            self.bad_rows += 1
            return False
        if not isinstance(chat, str) or not isinstance(name, str):
            self.bad_rows += 1
            return False

        self.rows += 1

        day = tsutil.utc_date(ts_us)
        self.by_day[day] = self.by_day.get(day, 0) + 1

        src_key = src if isinstance(src, str) else "?"
        self.by_src[src_key] = self.by_src.get(src_key, 0) + 1

        precision = obj.get("precision")
        prec_key = precision if isinstance(precision, str) else "?"
        self.by_precision[prec_key] = self.by_precision.get(prec_key, 0) + 1

        if isinstance(uuid, str) and uuid:
            self.uuid_fps.add(_fingerprint(uuid.lower()))
        else:
            self.null_uuid += 1
        self.name_fps.add(_fingerprint(name))

        component = obj.get("component")
        if isinstance(component, str) and component:
            self.with_component += 1

        if self.min_us is None or ts_us < self.min_us:
            self.min_us = ts_us
        if self.max_us is None or ts_us > self.max_us:
            self.max_us = ts_us

        ingest = obj.get("ingest_ts")
        if isinstance(ingest, str):
            try:
                ingest_us = tsutil.parse_ts(ingest)
            except tsutil.TsParseError:
                ingest_us = None
            if ingest_us is not None:
                if self.min_ingest_us is None or ingest_us < self.min_ingest_us:
                    self.min_ingest_us = ingest_us
                if self.max_ingest_us is None or ingest_us > self.max_ingest_us:
                    self.max_ingest_us = ingest_us

        length = len(chat)
        self.len_hist[length] = self.len_hist.get(length, 0) + 1
        self.len_total += length
        if length > self.len_max:
            self.len_max = length
        return True

    def mean_len(self) -> Optional[float]:
        """Mean chat length in characters, or None on an empty corpus."""
        if not self.rows:
            return None
        return self.len_total / float(self.rows)

    def median_len(self) -> Optional[float]:
        """Exact median chat length from the histogram (SPEC §11.3 sanity check).

        For an even row count this is the mean of the two central values, which
        is why the return type is a float. The histogram makes this exact without
        ever holding a per-row array.
        """
        if not self.rows:
            return None
        lower_index = (self.rows - 1) // 2
        upper_index = self.rows // 2
        lower = None  # type: Optional[int]
        upper = None  # type: Optional[int]
        seen = 0
        for length in sorted(self.len_hist):
            seen += self.len_hist[length]
            if lower is None and seen > lower_index:
                lower = length
            if seen > upper_index:
                upper = length
                break
        if lower is None or upper is None:  # pragma: no cover - rows > 0 guarantees both
            return None
        return (lower + upper) / 2.0

    def span_days(self) -> Optional[float]:
        """Event-time span of the corpus in days, or None if empty."""
        if self.min_us is None or self.max_us is None:
            return None
        return (self.max_us - self.min_us) / float(86400 * _US_PER_S)


def scan(paths: List[str], since_us: Optional[int], until_us: Optional[int]) -> Tuple[
    Accumulator, Dict[str, int]
]:
    """Stream `paths` into an :class:`Accumulator`. SPEC §11.3.

    `since_us` / `until_us` are inclusive bounds on event time, so a range here
    means the same thing it means to ``/chats/window`` (SPEC §7.3) and to
    ``export_csv``.
    """
    stats = {
        "files": 0,
        "lines": 0,
        "malformed": 0,
        "read_errors": 0,
        "filtered_time": 0,
    }
    acc = Accumulator()
    for obj in iter_rows(paths, stats):
        if since_us is not None or until_us is not None:
            ts_us = obj.get("ts_us")
            if isinstance(ts_us, int) and not isinstance(ts_us, bool):
                if since_us is not None and ts_us < since_us:
                    stats["filtered_time"] += 1
                    continue
                if until_us is not None and ts_us > until_us:
                    stats["filtered_time"] += 1
                    continue
        acc.add(obj)
    return acc, stats


def _fmt_us(us: Optional[int]) -> str:
    return tsutil.fmt_ts(us) if us is not None else "-"


def _fmt_num(value: Optional[float]) -> str:
    return "-" if value is None else "%.1f" % value


def to_json_obj(
    acc: Accumulator, stats: Dict[str, int], data_dir: str, days_limit: Optional[int]
) -> Dict[str, Any]:
    """Machine-readable form of the report. SPEC §11.3."""
    days = sorted(acc.by_day)
    if days_limit is not None and len(days) > days_limit:
        days = days[-days_limit:]
    return {
        "data_dir": data_dir,
        "files_scanned": stats["files"],
        "lines_read": stats["lines"],
        "rows": acc.rows,
        "malformed_lines": stats["malformed"],
        "unusable_rows": acc.bad_rows,
        "read_errors": stats["read_errors"],
        "rows_filtered_by_time": stats["filtered_time"],
        "rows_by_src": dict(acc.by_src),
        "rows_by_precision": dict(acc.by_precision),
        "rows_per_utc_day": dict((day, acc.by_day[day]) for day in days),
        "distinct_utc_days": len(acc.by_day),
        "distinct_player_uuids": len(acc.uuid_fps),
        "distinct_player_names": len(acc.name_fps),
        "null_uuid_rows": acc.null_uuid,
        "rows_with_component": acc.with_component,
        "min_ts": _fmt_us(acc.min_us) if acc.min_us is not None else None,
        "max_ts": _fmt_us(acc.max_us) if acc.max_us is not None else None,
        "min_ingest_ts": (
            _fmt_us(acc.min_ingest_us) if acc.min_ingest_us is not None else None
        ),
        "max_ingest_ts": (
            _fmt_us(acc.max_ingest_us) if acc.max_ingest_us is not None else None
        ),
        "event_span_days": acc.span_days(),
        "chat_len_mean": acc.mean_len(),
        "chat_len_median": acc.median_len(),
        "chat_len_max": acc.len_max if acc.rows else None,
    }


def render_table(
    acc: Accumulator,
    stats: Dict[str, int],
    data_dir: str,
    days_limit: Optional[int],
    out: IO[str],
) -> None:
    """Print the operator-facing table. SPEC §11.3, §11.4 (no chat content)."""
    out.write("corpus stats: %s\n" % (data_dir,))
    out.write("\n")
    out.write("  files scanned .............. %d\n" % stats["files"])
    out.write("  lines read ................. %d\n" % stats["lines"])
    out.write("  rows counted ............... %d\n" % acc.rows)
    if stats["filtered_time"]:
        out.write("  rows outside --since/--until %d\n" % stats["filtered_time"])
    out.write("  malformed lines ............ %d\n" % stats["malformed"])
    out.write("  unusable rows .............. %d\n" % acc.bad_rows)
    out.write("  read errors ................ %d\n" % stats["read_errors"])
    out.write("\n")
    out.write("  event ts range ............. %s .. %s\n"
              % (_fmt_us(acc.min_us), _fmt_us(acc.max_us)))
    out.write("  ingest ts range ............ %s .. %s\n"
              % (_fmt_us(acc.min_ingest_us), _fmt_us(acc.max_ingest_us)))
    span = acc.span_days()
    out.write("  event span (days) .......... %s\n" % _fmt_num(span))
    out.write("  distinct UTC days .......... %d\n" % len(acc.by_day))
    out.write("  distinct player uuids ...... %d\n" % len(acc.uuid_fps))
    out.write("  distinct player names ...... %d\n" % len(acc.name_fps))
    out.write("  rows with null uuid ........ %d\n" % acc.null_uuid)
    out.write("  rows with a component ...... %d\n" % acc.with_component)
    out.write("  chat length mean/median/max  %s / %s / %s\n" % (
        _fmt_num(acc.mean_len()),
        _fmt_num(acc.median_len()),
        acc.len_max if acc.rows else "-",
    ))

    out.write("\n  rows by src\n")
    if not acc.by_src:
        out.write("    (none)\n")
    for key in sorted(acc.by_src, key=lambda k: (-acc.by_src[k], k)):
        count = acc.by_src[key]
        share = 100.0 * count / acc.rows if acc.rows else 0.0
        out.write("    %-14s %10d  %5.1f%%\n" % (key, count, share))

    out.write("\n  rows by precision\n")
    if not acc.by_precision:
        out.write("    (none)\n")
    for key in sorted(acc.by_precision, key=lambda k: (-acc.by_precision[k], k)):
        out.write("    %-14s %10d\n" % (key, acc.by_precision[key]))

    days = sorted(acc.by_day)
    hidden = 0
    if days_limit is not None and len(days) > days_limit:
        hidden = len(days) - days_limit
        days = days[-days_limit:]
    out.write("\n  rows per UTC day (by event ts)\n")
    if hidden:
        out.write("    ... %d earlier day(s) hidden; --days 0 shows all\n" % hidden)
    if not days:
        out.write("    (none)\n")
    peak = max((acc.by_day[d] for d in days), default=0)
    for day in days:
        count = acc.by_day[day]
        bar = "#" * int(round(40.0 * count / peak)) if peak else ""
        out.write("    %s %8d  %s\n" % (day, count, bar))


def _parse_bound(value: Optional[str], flag: str) -> Optional[int]:
    """Parse a --since/--until argument through tsutil (SPEC §3.1).

    Exits 2 (bad arguments) rather than raising, so an unparseable bound is never
    confused with the exit-1 "corpus is empty" canary.
    """
    if value is None:
        return None
    try:
        return tsutil.parse_ts(value)
    except tsutil.TsParseError as exc:
        _warn("%s: %s" % (flag, exc))
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    """Command line for the corpus report. SPEC §11.3."""
    parser = argparse.ArgumentParser(
        prog="python tools/corpus_stats.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Summarise the corpus: rows per UTC day, rows by src, distinct\n"
            "players, ts range, null uuids, chat-length mean/median. Read-only."
        ),
        epilog=(
            "WHEN YOU WANT THIS\n"
            "  After the first hour, the first day, and the first week, then\n"
            "  whenever you want to confirm collection is real rather than merely\n"
            "  running. --health proves the process is alive; this proves rows are\n"
            "  landing, in the right shape, on every day they should.\n"
            "\n"
            "WHAT TO LOOK FOR\n"
            "  * rows per UTC day should be broadly flat, order 10k-20k/day\n"
            "    (6.7M rows/year, SPEC 11.1). A day at a third of its neighbours\n"
            "    is a hole -- feed that range to tools/enqueue_gap.py.\n"
            "  * NO MISSING DAYS between min and max ts. A gap in the day list is\n"
            "    the loudest possible signal.\n"
            "  * rows by src: rest-backfill should be a small slice of sse. A\n"
            "    large one means the stream is unhealthy and backfill is carrying\n"
            "    the corpus.\n"
            "  * rows with null uuid should be ~0 on modern data. A sudden climb\n"
            "    is the SPEC 2.1 field-rename trap: playerUuid on SSE vs uuid on\n"
            "    the window endpoint.\n"
            "  * chat length mean/median in the low tens of characters. A median\n"
            "    of 0 means empty bodies are being stored.\n"
            "\n"
            "EXAMPLES\n"
            "  python tools/corpus_stats.py\n"
            "  python tools/corpus_stats.py --days 0\n"
            "  python tools/corpus_stats.py --since 2026-07-01T00:00:00Z --json\n"
            "\n"
            "EXIT CODES\n"
            "  0 rows were counted    1 nothing matched (empty corpus -- use as a\n"
            "  cron canary)           2 bad arguments or no data_dir\n"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="collector.json to read data_dir from (default: built-in defaults "
        "plus any CHATCOL_* environment overrides)",
    )
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        default=None,
        help="corpus directory, overriding the configured data_dir",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        default=None,
        help="only count rows with event ts >= this instant (inclusive)",
    )
    parser.add_argument(
        "--until",
        metavar="ISO",
        default=None,
        help="only count rows with event ts <= this instant (inclusive)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=45,
        metavar="N",
        help="show only the most recent N days in the per-day table "
        "(0 = all, default 45). Totals always cover everything scanned",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the table",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``python tools/corpus_stats.py``. SPEC §11.3."""
    args = build_parser().parse_args(argv)

    try:
        cfg = config.load_config(args.config)
    except config.ConfigError as exc:
        _warn(str(exc))
        return 2
    data_dir = args.data_dir or cfg["data_dir"]
    if not os.path.isdir(data_dir):
        _warn("no such data_dir: %s" % (data_dir,))
        return 2

    since_us = _parse_bound(args.since, "--since")
    until_us = _parse_bound(args.until, "--until")
    if since_us is not None and until_us is not None and until_us < since_us:
        _warn("--until is earlier than --since")
        return 2

    days_limit = None if args.days <= 0 else args.days
    acc, stats = scan(corpus_files(data_dir), since_us, until_us)

    if args.json:
        json.dump(
            to_json_obj(acc, stats, data_dir, days_limit),
            sys.stdout,
            indent=1,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        render_table(acc, stats, data_dir, days_limit, sys.stdout)

    return 0 if acc.rows else 1


if __name__ == "__main__":
    sys.exit(main())
