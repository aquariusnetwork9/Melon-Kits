#!/usr/bin/env python3
"""Export the JSONL corpus as a dump-compatible CSV (SPEC §3.4).

Produces a file that concatenates **byte-compatibly** with rfresh's bulk
`chats.csv`: header exactly ``time,chat,player_name,player_uuid``, LF line
endings, no BOM, RFC4180 quoting (a field is quoted only when it contains a
comma, a double quote, CR or LF; an embedded quote is escaped by doubling),
and timestamps converted through :func:`tsutil.to_dump_csv_ts` -- ``T`` becomes
a space, ``Z`` becomes ``+00``, trailing zeros are stripped from the fraction
and the decimal point disappears with them.

The corpus is streamed one line at a time and rows are written as they are read:
these files reach gigabytes, and nothing here ever holds more than one record.

Privacy (SPEC §11.4): chat content goes to the CSV and nowhere else. Every
diagnostic on stderr is counters and file basenames only, so redirecting stdout
to a file and watching stderr never leaks a message body. Malformed lines are
counted, never echoed.
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
from typing import Any, Dict, IO, Iterator, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402  (path bootstrap must run first)
import record  # noqa: E402
import tsutil  # noqa: E402

#: SPEC §3.4 -- the dump's header, byte for byte.
HEADER = b"time,chat,player_name,player_uuid\n"

#: SPEC §5.1/§5.2 file names: the daily file, its gzipped form, and the
#: ``max_file_bytes`` failsafe variant. Deliberately anchored so that a stray
#: ``chat-2026-07-25.jsonl.gz.tmp`` (a compression in flight, SPEC §5.3) is
#: never read as corpus data.
_FILE_RE = re.compile(r"^chat-\d{4}-\d{2}-\d{2}(?:\.\d+)?\.jsonl(?:\.gz)?$")

#: RFC4180: these four characters, and only these, force quoting.
_QUOTE_TRIGGERS = (",", '"', "\r", "\n")

_DEFAULT_SRCS = (record.SRC_SSE, record.SRC_BACKFILL)

_FLUSH_ROWS = 2000


def _warn(msg: str) -> None:
    """Write a metadata-only diagnostic to stderr (SPEC §11.4)."""
    sys.stderr.write("export_csv: %s\n" % (msg,))


def new_stats() -> Dict[str, int]:
    """Counter block shared by the reader and the summary. SPEC §11.4."""
    return {
        "files": 0,
        "lines": 0,
        "malformed": 0,
        "read_errors": 0,
        "filtered_src": 0,
        "filtered_time": 0,
        "written": 0,
    }


def corpus_files(data_dir: str) -> List[str]:
    """Every corpus data file in `data_dir`, oldest first (SPEC §5.1, §5.2).

    Globs ``chat-*.jsonl*`` and keeps only names matching the canonical
    pattern, so ``.gz.tmp`` work-in-progress files are skipped. Plain and
    gzipped days sort together by basename, which is chronological because the
    date is fixed-width.
    """
    found = []  # type: List[str]
    for path in glob.glob(os.path.join(data_dir, "chat-*.jsonl*")):
        if _FILE_RE.match(os.path.basename(path)):
            found.append(path)
    found.sort(key=lambda p: os.path.basename(p))
    return found


def open_corpus_file(path: str) -> IO[bytes]:
    """Open one corpus file for binary line iteration (SPEC §5.3).

    The reader picks the decoder by suffix: ``.gz`` through :mod:`gzip`,
    anything else raw. Both are streamed.
    """
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def iter_rows(paths: List[str], stats: Dict[str, int]) -> Iterator[Dict[str, Any]]:
    """Yield one decoded record per corpus line, streaming (SPEC §5.5).

    A line that will not decode as a JSON object increments ``malformed`` and is
    skipped -- it is never logged, because a torn line still contains chat text
    (SPEC §11.4). A truncated gzip member ends that file and increments
    ``read_errors`` rather than aborting the export.
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


def csv_field(value: str) -> str:
    """Quote one field per RFC4180, as the dump does (SPEC §3.4).

    Quotes are added only when the value contains a comma, a double quote, CR or
    LF; an embedded double quote is escaped by doubling it. Anything else is
    emitted bare, which is what makes the output byte-compatible with
    ``chats.csv`` instead of merely parseable.
    """
    for ch in _QUOTE_TRIGGERS:
        if ch in value:
            return '"' + value.replace('"', '""') + '"'
    return value


def csv_line(ts_us: int, chat: str, name: str, uuid: Optional[str]) -> bytes:
    """Render one dump-compatible CSV record, LF terminated (SPEC §3.4).

    A ``null`` ``player_uuid`` becomes an empty unquoted field, which is how
    Postgres' CSV output (and therefore the bulk dump) represents SQL NULL.
    """
    fields = (
        tsutil.to_dump_csv_ts(ts_us),
        csv_field(chat),
        csv_field(name),
        "" if uuid is None else csv_field(uuid),
    )
    return (",".join(fields) + "\n").encode("utf-8")


def _row_values(
    obj: Dict[str, Any]
) -> Optional[Tuple[int, str, str, Optional[str], str]]:
    """Pull (ts_us, chat, player_name, player_uuid, src) or None if unusable.

    SPEC §2. Type-checks every field it needs; a row that fails is counted as
    malformed by the caller and never rendered.
    """
    ts_us = obj.get("ts_us")
    chat = obj.get("chat")
    name = obj.get("player_name")
    uuid = obj.get("player_uuid")
    src = obj.get("src")
    if isinstance(ts_us, bool) or not isinstance(ts_us, int):
        return None
    if not isinstance(chat, str) or not isinstance(name, str):
        return None
    if uuid is not None and not isinstance(uuid, str):
        return None
    if not isinstance(src, str):
        return None
    return ts_us, chat, name, uuid, src


def export(
    data_dir: str,
    out: IO[bytes],
    srcs: Tuple[str, ...],
    since_us: Optional[int],
    until_us: Optional[int],
    write_header: bool,
    stats: Dict[str, int],
) -> None:
    """Stream the corpus into `out` as dump-compatible CSV (SPEC §3.4).

    `since_us` / `until_us` are inclusive bounds on the event time ``ts_us``,
    matching the inclusive semantics of ``/chats/window`` (SPEC §7.3) so an
    operator can re-export the same range they backfilled. Rows are emitted in
    corpus order, i.e. receive order (SPEC §6.6) -- sort downstream by ``time``
    if chronological order matters.
    """
    if write_header:
        out.write(HEADER)
    allowed = frozenset(srcs)
    pending = []  # type: List[bytes]
    for obj in iter_rows(corpus_files(data_dir), stats):
        values = _row_values(obj)
        if values is None:
            stats["malformed"] += 1
            continue
        ts_us, chat, name, uuid, src = values
        if src not in allowed:
            stats["filtered_src"] += 1
            continue
        if since_us is not None and ts_us < since_us:
            stats["filtered_time"] += 1
            continue
        if until_us is not None and ts_us > until_us:
            stats["filtered_time"] += 1
            continue
        pending.append(csv_line(ts_us, chat, name, uuid))
        stats["written"] += 1
        if len(pending) >= _FLUSH_ROWS:
            out.write(b"".join(pending))
            del pending[:]
    if pending:
        out.write(b"".join(pending))
    out.flush()


def _parse_bound(value: Optional[str], flag: str) -> Optional[int]:
    """Parse a --since/--until argument through tsutil (SPEC §3.1).

    Exits 2 (bad arguments) rather than raising, so the caller does not have to
    distinguish an unparseable bound from an empty corpus.
    """
    if value is None:
        return None
    try:
        return tsutil.parse_ts(value)
    except tsutil.TsParseError as exc:
        _warn("%s: %s" % (flag, exc))
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    """Command line for the CSV exporter. SPEC §3.4."""
    parser = argparse.ArgumentParser(
        prog="python tools/export_csv.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Convert the JSONL corpus to a CSV that concatenates byte-compatibly\n"
            "with rfresh's bulk chats.csv (SPEC 3.4)."
        ),
        epilog=(
            "WHEN YOU WANT THIS\n"
            "  You have the bulk chats.csv covering history up to the day you\n"
            "  started collecting, and you want one continuous file: export the\n"
            "  corpus with --no-header and cat it onto the end of the dump. Also\n"
            "  useful for feeding tooling that already speaks the dump's four\n"
            "  columns and knows nothing about the 12-key JSONL schema.\n"
            "\n"
            "WHAT IT DROPS\n"
            "  Everything the dump has no column for: component (available\n"
            "  nowhere else, SPEC 9), src, precision, row_id, ingest_ts, seq,\n"
            "  batch. This is an export, never an archive -- keep the JSONL.\n"
            "\n"
            "ROW ORDER\n"
            "  Corpus order, which is receive order (SPEC 6.6): a backfilled row\n"
            "  for Tuesday sits in Thursday's file. Sort by the time column\n"
            "  downstream if you need chronological order.\n"
            "\n"
            "EXAMPLES\n"
            "  python tools/export_csv.py -o chats-new.csv\n"
            "  python tools/export_csv.py --no-header --since 2026-07-01T00:00:00Z \\\n"
            "      >> chats.csv\n"
            "  python tools/export_csv.py --include-proxy-log -o everything.csv\n"
            "\n"
            "EXIT CODES\n"
            "  0 rows were written    1 no rows matched    2 bad arguments\n"
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
        "-o",
        "--out",
        metavar="PATH",
        required=True,
        help="output file, written as raw bytes so LF endings survive on Windows. "
        "REQUIRED: this is the one tool that emits chat text, and 2b2t chat routinely "
        "contains coordinates, so it must never land in a terminal or shell scrollback "
        "by accident (SPEC 11.4). Pass '-' if you genuinely want stdout for a pipe.",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="omit the header line, for appending to an existing chats.csv",
    )
    parser.add_argument(
        "--include-proxy-log",
        action="store_true",
        help="also export src=proxy-log rows. Off by default: log rows are "
        "name-only, second-resolution and never authoritative (SPEC 8.5), and "
        "the dump's four columns cannot express that difference",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        default=None,
        help="only rows with event ts >= this instant (inclusive), e.g. "
        "2026-07-01T00:00:00Z. A missing zone means UTC",
    )
    parser.add_argument(
        "--until",
        metavar="ISO",
        default=None,
        help="only rows with event ts <= this instant (inclusive)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the counter summary on stderr",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``python tools/export_csv.py``. SPEC §3.4."""
    args = build_parser().parse_args(argv)

    try:
        cfg = config.load_config(args.config)
    except config.ConfigError as exc:
        sys.stderr.write("export_csv: %s\n" % (exc,))
        return 2
    data_dir = args.data_dir or cfg["data_dir"]

    since_us = _parse_bound(args.since, "--since")
    until_us = _parse_bound(args.until, "--until")
    if since_us is not None and until_us is not None and until_us < since_us:
        sys.stderr.write("export_csv: --until is earlier than --since\n")
        return 2

    if not os.path.isdir(data_dir):
        sys.stderr.write("export_csv: no such data_dir: %s\n" % (data_dir,))
        return 2

    srcs = _DEFAULT_SRCS
    if args.include_proxy_log:
        srcs = srcs + (record.SRC_LOG,)

    stats = new_stats()
    if args.out == "-":
        # Explicit opt-in only. `-o` is required precisely so that a bare invocation can
        # never dump raw chat -- coordinates included -- into a terminal and its scrollback
        # (SPEC 11.4). Warn on a tty, where "-" is almost certainly a mistake rather than
        # a deliberate pipe into another program.
        if sys.stdout.isatty():
            sys.stderr.write(
                "export_csv: refusing to write chat text to a terminal.\n"
                "  '-o -' is for piping into another program. Redirect it, or use "
                "'-o FILE'.\n")
            return 2
        export(
            data_dir,
            sys.stdout.buffer,
            srcs,
            since_us,
            until_us,
            not args.no_header,
            stats,
        )
    else:
        with open(args.out, "wb") as handle:
            export(
                data_dir,
                handle,
                srcs,
                since_us,
                until_us,
                not args.no_header,
                stats,
            )

    if not args.quiet:
        _warn(
            "files=%d lines=%d written=%d skipped_src=%d skipped_time=%d "
            "malformed=%d read_errors=%d"
            % (
                stats["files"],
                stats["lines"],
                stats["written"],
                stats["filtered_src"],
                stats["filtered_time"],
                stats["malformed"],
                stats["read_errors"],
            )
        )
    return 0 if stats["written"] else 1


if __name__ == "__main__":
    sys.exit(main())
