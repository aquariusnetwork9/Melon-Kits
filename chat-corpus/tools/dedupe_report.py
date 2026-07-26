#!/usr/bin/env python3
"""Corpus-wide duplicate `row_id` audit (SPEC §4.3d).

The collector's own dedupe is bounded by event time (SPEC §4.3a) and rehydrated
from the corpus on start (§4.3b), so it cannot see a duplicate pair separated by
more than ``dedupe.window_s``. Months of unattended running deserve an auditable
check that has no window at all. This is that check. Run it monthly; it is a
read-only reader and is safe to run while the collector is live.

MEMORY STRATEGY -- two passes over a fixed-size fingerprint sieve
-----------------------------------------------------------------
Holding every ``row_id`` in a set costs ~100 B/row, which is ~700 MB/year of
corpus, so that is not an option on a small VPS. Instead:

* **Pass 1** hashes each ``row_id`` to a bucket in two fixed-size bit arrays:
  ``seen`` (this bucket has occurred) and ``dup`` (this bucket has occurred at
  least twice). Memory is exactly ``2 * 2**(bits-3)`` bytes -- 64 MiB at the
  default ``--bits 28`` -- **independent of corpus size**. ``dup`` is a superset
  of the truly duplicated row_ids plus bucket-collision false positives.
* **Pass 2** re-reads the corpus and tracks exact per-``row_id`` detail only for
  rows whose bucket is marked in ``dup``. False positives fall out at the end
  because their exact count is 1. Memory here is proportional to the number of
  *candidates*, not to the corpus: roughly ``n**2 / 2**(bits+1)`` collisions,
  i.e. ~170k entries (~30 MB) for a year of chat at the default sieve size.

The result is exact -- no false positives and no false negatives -- at a fixed
memory ceiling and the cost of reading the corpus twice. Raise ``--bits`` by one
for every doubling of the corpus to keep pass 2's candidate set small.

Privacy (SPEC §11.4): this tool reads chat text and reports none of it. Output is
``row_id``, ``ts``, ``src`` and counters only, which is exactly the metadata set
the standing operator order permits.
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
from typing import Any, Dict, IO, Iterator, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402  (path bootstrap must run first)
import tsutil  # noqa: E402

#: SPEC §5.1/§5.2 corpus file names; ``.gz.tmp`` work in progress is excluded.
_FILE_RE = re.compile(r"^chat-\d{4}-\d{2}-\d{2}(?:\.\d+)?\.jsonl(?:\.gz)?$")

#: Default sieve size: 2**28 buckets = 32 MiB per bit array, 64 MiB total.
DEFAULT_BITS = 28

#: Hard ceiling on tracked candidate groups in pass 2, so a pathologically
#: undersized sieve degrades into a reported truncation instead of an OOM kill.
DEFAULT_MAX_GROUPS = 2000000

_MIN_BITS = 12
_MAX_BITS = 34

EXIT_CLEAN = 0
EXIT_DUPLICATES = 1
EXIT_ERROR = 2


def _warn(msg: str) -> None:
    """Metadata-only diagnostic on stderr (SPEC §11.4)."""
    sys.stderr.write("dedupe_report: %s\n" % (msg,))


def new_stats() -> Dict[str, int]:
    """Counter block for both passes. SPEC §11.4 -- counters, never content."""
    return {
        "files": 0,
        "lines": 0,
        "malformed": 0,
        "no_row_id": 0,
        "read_errors": 0,
        "rows": 0,
    }


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

    Never holds more than one line. Undecodable lines are counted, never echoed
    (SPEC §11.4); a truncated gzip member ends that file and continues.
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


class Sieve:
    """Fixed-size two-bit-array duplicate sieve. SPEC §4.3d, see module docstring.

    ``add()`` returns True the second and every later time a bucket is touched.
    ``is_candidate()`` answers the pass-2 question "could this row_id be a
    duplicate?" -- always True for a real duplicate, sometimes True for a
    bucket collision, which pass 2 resolves exactly.
    """

    __slots__ = ("_seen", "_dup", "_mask")

    def __init__(self, bits: int) -> None:
        if not _MIN_BITS <= bits <= _MAX_BITS:
            raise ValueError("bits must be between %d and %d" % (_MIN_BITS, _MAX_BITS))
        size = 1 << (bits - 3)
        self._seen = bytearray(size)
        self._dup = bytearray(size)
        self._mask = (1 << bits) - 1

    @property
    def bytes_used(self) -> int:
        """Total bytes of the two bit arrays -- the tool's memory ceiling."""
        return len(self._seen) + len(self._dup)

    def bucket(self, row_id: str) -> int:
        """Map a row_id onto a sieve bucket.

        ``row_id`` is already a truncated sha256 (SPEC §4.2), but proxy-log ids
        carry an ``L`` prefix and are 33 chars, so the whole string is rehashed
        rather than sliced -- ``blake2b`` with an 8-byte digest is fast and gives
        both namespaces a uniform spread.
        """
        digest = blake2b(row_id.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") & self._mask

    def add(self, row_id: str) -> bool:
        """Record a row_id. True once its bucket has been seen more than once."""
        index = self.bucket(row_id)
        byte, bit = index >> 3, 1 << (index & 7)
        if self._seen[byte] & bit:
            self._dup[byte] |= bit
            return True
        self._seen[byte] |= bit
        return False

    def is_candidate(self, row_id: str) -> bool:
        """True when this row_id's bucket was hit at least twice in pass 1."""
        index = self.bucket(row_id)
        return bool(self._dup[index >> 3] & (1 << (index & 7)))


def _row_id_of(obj: Dict[str, Any], stats: Dict[str, int]) -> Optional[str]:
    """Extract a usable row_id, counting rows that have none (SPEC §2 key 9)."""
    rid = obj.get("row_id")
    if not isinstance(rid, str) or not rid:
        stats["no_row_id"] += 1
        return None
    return rid


def pass_one(paths: List[str], sieve: Sieve, stats: Dict[str, int]) -> int:
    """Populate the sieve; return the number of buckets flagged. SPEC §4.3d."""
    flagged = 0
    for obj in iter_rows(paths, stats):
        rid = _row_id_of(obj, stats)
        if rid is None:
            continue
        stats["rows"] += 1
        if sieve.add(rid):
            flagged += 1
    return flagged


def pass_two(
    paths: List[str], sieve: Sieve, stats: Dict[str, int], max_groups: int
) -> Dict[str, Any]:
    """Resolve sieve candidates into exact duplicate groups. SPEC §4.3d.

    Each tracked group records its exact occurrence count, the first and last
    ``ts_us`` seen for it, and a per-``src`` breakdown -- which is what tells the
    operator whether a duplicate pair is SSE-vs-backfill overlap (benign, and a
    dedupe bug if it reaches disk) or the same source twice (a replay).
    """
    groups = {}  # type: Dict[str, Dict[str, Any]]
    truncated = 0
    for obj in iter_rows(paths, stats):
        rid = _row_id_of(obj, stats)
        if rid is None or not sieve.is_candidate(rid):
            continue
        entry = groups.get(rid)
        if entry is None:
            if len(groups) >= max_groups:
                truncated += 1
                continue
            entry = {"count": 0, "first_us": None, "last_us": None, "src": {}}
            groups[rid] = entry
        entry["count"] += 1
        ts_us = obj.get("ts_us")
        if isinstance(ts_us, int) and not isinstance(ts_us, bool):
            if entry["first_us"] is None or ts_us < entry["first_us"]:
                entry["first_us"] = ts_us
            if entry["last_us"] is None or ts_us > entry["last_us"]:
                entry["last_us"] = ts_us
        src = obj.get("src")
        key = src if isinstance(src, str) else "?"
        entry["src"][key] = entry["src"].get(key, 0) + 1
    dupes = dict((rid, e) for rid, e in groups.items() if e["count"] > 1)
    return {
        "groups": dupes,
        "candidates": len(groups),
        "truncated_rows": truncated,
    }


def _fmt_group(rid: str, entry: Dict[str, Any]) -> str:
    """One report line: row_id, count, per-src counts, first/last ts."""
    srcs = ",".join(
        "%s=%d" % (name, entry["src"][name]) for name in sorted(entry["src"])
    )
    first = tsutil.fmt_ts(entry["first_us"]) if entry["first_us"] is not None else "-"
    last = tsutil.fmt_ts(entry["last_us"]) if entry["last_us"] is not None else "-"
    return "%-34s n=%-4d %-28s first=%s last=%s" % (
        rid,
        entry["count"],
        srcs,
        first,
        last,
    )


def report(
    result: Dict[str, Any],
    stats: Dict[str, int],
    sieve: Sieve,
    limit: Optional[int],
    as_json: bool,
    out: IO[str],
) -> None:
    """Print the audit result. SPEC §4.3d, §11.4 (no chat content, ever)."""
    groups = result["groups"]
    ordered = sorted(
        groups.items(),
        key=lambda kv: (-kv[1]["count"], kv[1]["first_us"] or 0, kv[0]),
    )
    extra_rows = sum(e["count"] - 1 for e in groups.values())
    shown = ordered if limit is None else ordered[:limit]

    if as_json:
        payload = {
            "rows_scanned": stats["rows"],
            "lines_read": stats["lines"],
            "files": stats["files"],
            "malformed_lines": stats["malformed"],
            "rows_without_row_id": stats["no_row_id"],
            "read_errors": stats["read_errors"],
            "sieve_bytes": sieve.bytes_used,
            "sieve_candidates": result["candidates"],
            "candidate_rows_untracked": result["truncated_rows"],
            "duplicate_groups": len(groups),
            "duplicate_extra_rows": extra_rows,
            "groups": [
                {
                    "row_id": rid,
                    "count": entry["count"],
                    "src": entry["src"],
                    "first_ts": (
                        tsutil.fmt_ts(entry["first_us"])
                        if entry["first_us"] is not None
                        else None
                    ),
                    "last_ts": (
                        tsutil.fmt_ts(entry["last_us"])
                        if entry["last_us"] is not None
                        else None
                    ),
                }
                for rid, entry in shown
            ],
            "groups_truncated": limit is not None and len(ordered) > len(shown),
        }
        json.dump(payload, out, indent=1, sort_keys=True)
        out.write("\n")
        return

    out.write("corpus dedupe audit (SPEC 4.3d)\n")
    out.write("  files scanned .......... %d\n" % stats["files"])
    out.write("  lines read ............. %d\n" % stats["lines"])
    out.write("  rows with a row_id ..... %d\n" % stats["rows"])
    out.write("  malformed lines ........ %d\n" % stats["malformed"])
    out.write("  rows without row_id .... %d\n" % stats["no_row_id"])
    out.write("  read errors ............ %d\n" % stats["read_errors"])
    out.write("  sieve memory ........... %d bytes\n" % sieve.bytes_used)
    out.write("  sieve candidates ....... %d\n" % result["candidates"])
    out.write("  duplicate row_ids ...... %d\n" % len(groups))
    out.write("  redundant rows ......... %d\n" % extra_rows)
    if result["truncated_rows"]:
        out.write(
            "  UNTRACKED candidate rows %d  (--max-groups hit; raise --bits)\n"
            % result["truncated_rows"]
        )
    if not groups:
        out.write("\nno duplicate row_id in the corpus.\n")
        return
    out.write("\nduplicate groups (worst first):\n")
    for rid, entry in shown:
        out.write("  " + _fmt_group(rid, entry) + "\n")
    if len(ordered) > len(shown):
        out.write(
            "  ... %d more group(s); use --limit 0 to list them all\n"
            % (len(ordered) - len(shown))
        )


def build_parser() -> argparse.ArgumentParser:
    """Command line for the duplicate audit. SPEC §4.3d."""
    parser = argparse.ArgumentParser(
        prog="python tools/dedupe_report.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Stream the whole corpus and report duplicate row_ids in fixed memory\n"
            "(SPEC 4.3d). Read-only; safe to run against a live corpus."
        ),
        epilog=(
            "WHEN YOU WANT THIS\n"
            "  Monthly, from cron, as the belt-and-braces check on the\n"
            "  collector's bounded in-memory dedupe. Also the first thing to run\n"
            "  after a hard kill, a disk-full event, or restoring from a backup:\n"
            "  it is the only check that sees the entire corpus at once.\n"
            "  A clean run is silent evidence that the dedupe window, the\n"
            "  hwm-anchored rehydration (SPEC 4.3b) and the inclusive\n"
            "  /chats/window boundary row (SPEC 7.3) are all behaving.\n"
            "\n"
            "READING THE OUTPUT\n"
            "  Any duplicate is a defect -- the row_id is the dedupe key, so a\n"
            "  repeat means two writes got past the writer. The per-src counts\n"
            "  say which path did it: sse=2 is a stream replay that outran the\n"
            "  dedupe window; sse=1,rest-backfill=1 is a backfill overlap that\n"
            "  should have been discarded at ingest.\n"
            "\n"
            "MEMORY\n"
            "  Two passes over a fixed bit sieve: 2 * 2**(bits-3) bytes, 64 MiB\n"
            "  at the default --bits 28, regardless of corpus size. Pass 2 then\n"
            "  holds only sieve candidates. Add 1 to --bits per doubling of the\n"
            "  corpus to keep the candidate set small.\n"
            "\n"
            "EXAMPLES\n"
            "  python tools/dedupe_report.py\n"
            "  python tools/dedupe_report.py --bits 30 --limit 0\n"
            "  python tools/dedupe_report.py --json > dedupe-$(date -u +%F).json\n"
            "\n"
            "EXIT CODES  (cron-friendly)\n"
            "  0 no duplicates\n"
            "  1 duplicates found\n"
            "  2 the audit could not be completed (bad args, unreadable corpus,\n"
            "    or candidate tracking truncated -- the answer is not trustworthy)\n"
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
        "--bits",
        type=int,
        default=DEFAULT_BITS,
        metavar="N",
        help="sieve size as a power of two (%d..%d, default %d = 64 MiB total)"
        % (_MIN_BITS, _MAX_BITS, DEFAULT_BITS),
    )
    parser.add_argument(
        "--max-groups",
        type=int,
        default=DEFAULT_MAX_GROUPS,
        metavar="N",
        help="ceiling on candidate groups tracked in pass 2 (default %d). "
        "Hitting it means --bits is too small; the run then exits 2"
        % DEFAULT_MAX_GROUPS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="list at most N duplicate groups (0 = all, default 50)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON instead of a table",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``python tools/dedupe_report.py``. SPEC §4.3d."""
    args = build_parser().parse_args(argv)

    try:
        cfg = config.load_config(args.config)
    except config.ConfigError as exc:
        _warn(str(exc))
        return EXIT_ERROR
    data_dir = args.data_dir or cfg["data_dir"]
    if not os.path.isdir(data_dir):
        _warn("no such data_dir: %s" % (data_dir,))
        return EXIT_ERROR
    if args.max_groups < 1:
        _warn("--max-groups must be at least 1")
        return EXIT_ERROR

    try:
        sieve = Sieve(args.bits)
    except (ValueError, MemoryError) as exc:
        _warn("cannot allocate sieve: %s" % (exc,))
        return EXIT_ERROR

    paths = corpus_files(data_dir)
    if not paths:
        _warn("no corpus files (chat-*.jsonl*) in %s" % (data_dir,))
        return EXIT_ERROR

    stats = new_stats()
    pass_one(paths, sieve, stats)

    pass2_stats = new_stats()
    result = pass_two(paths, sieve, pass2_stats, args.max_groups)
    # Pass 2 re-reads the same files; report pass 1's scan counters, which are
    # the authoritative view of the corpus, and keep pass 2's read errors.
    stats["read_errors"] += pass2_stats["read_errors"]

    limit = None if args.limit <= 0 else args.limit
    report(result, stats, sieve, limit, args.json, sys.stdout)

    if result["truncated_rows"] or stats["read_errors"]:
        return EXIT_ERROR
    return EXIT_DUPLICATES if result["groups"] else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
