#!/usr/bin/env python3
"""Inject a backfill gap into `state.json` by hand (SPEC §11.2, §7.1).

The collector heals its own downtime: on start it enqueues
``[hwm_us - safety_lag, now + 5s]`` and the backfill worker walks it (SPEC §11.2
step 6). That automatic gap is **clamped to ``backfill.max_lookback_days``**
(default 7) by :meth:`Writer.enqueue_gap`, because an absent or corrupt
``hwm_us`` would otherwise trigger a crawl back to 2013 (SPEC §7.9). So downtime
longer than the clamp cannot be healed automatically -- and that is exactly and
only what this tool is for. It writes the gap straight into ``state.json``, past
the clamp, with its own sanity ceiling (``--max-span-days``) in place of it.

Two refusals are load-bearing:

* **A zero-or-negative span is refused.** ``startDate == endDate`` returns HTTP
  400, not an empty result (SPEC §6.5), and a 400 is treated as a permanent bug
  that alarms and never retries (SPEC §7.5). Enqueuing one would manufacture a
  ``gaps_error`` and flip health to critical.
* **Running while the collector holds the lock is refused.** ``state.json`` has
  exactly one writer by design (SPEC §10); the live collector holds the whole
  document in memory and rewrites it atomically, so anything this tool wrote
  would be silently overwritten at the next persist -- losing the gap and,
  worse, looking like it worked.

Privacy (SPEC §11.4): this tool never reads the corpus and never touches chat
text. Output is timestamps, counters and gap ids.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402  (path bootstrap must run first)
import singlelock  # noqa: E402
import statefile  # noqa: E402
import tsutil  # noqa: E402

_US_PER_S = 1000000
_US_PER_DAY = 86400 * _US_PER_S

#: SPEC §7.1 ids look like ``g000417``; a manual gap keeps the same shape so the
#: ``batch`` field on every backfilled row stays uniform (SPEC §2 key 12).
_GAP_ID_RE = re.compile(r"^g(\d+)$")
_GAP_ID_FMT = "g%06d"

#: Sanity ceiling in place of the clamp this tool deliberately bypasses. A typo
#: in the year is the failure mode: 2016 instead of 2026 would enqueue a decade.
DEFAULT_MAX_SPAN_DAYS = 90

#: The oldest chat in the database (SPEC §7.9). Earlier than this is a typo.
_EARLIEST_CHAT_US = tsutil.parse_ts("2013-11-18T00:00:00Z")

#: SPEC §12.14 -- a window anchored inside the last hour would 400 if the
#: maintainer ever enables the commented-out scraper guard.
_RECENT_GUARD_US = 3600 * _US_PER_S

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_RUNNING = 3
EXIT_NO_STATE = 4


def _make_logger() -> logging.Logger:
    """stderr logger for statefile's metadata-only diagnostics (SPEC §11.4)."""
    log = logging.getLogger("enqueue_gap")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("enqueue_gap: %(levelname)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    return log


def _err(msg: str) -> None:
    """One-line failure on stderr."""
    sys.stderr.write("enqueue_gap: %s\n" % (msg,))


def parse_bound(value: str, flag: str) -> int:
    """Validate an operator-supplied ISO instant through tsutil (SPEC §3.1).

    Accepts everything the corpus parser accepts: ``T`` or a space separator,
    0-9 fractional digits, and ``Z`` / ``+HH:MM`` / no zone at all -- a missing
    zone means **UTC**, never local time, which is the one assumption an operator
    is most likely to get wrong here.

    Exits :data:`EXIT_USAGE` on a bad instant; the offending value is not echoed
    beyond its length, because tsutil's own error text is metadata only.
    """
    try:
        return tsutil.parse_ts(value)
    except tsutil.TsParseError as exc:
        _err("%s is not a parseable instant: %s" % (flag, exc))
        raise SystemExit(EXIT_USAGE)


def next_gap_id(gaps: List[Any]) -> str:
    """Allocate an unused ``g######`` id (SPEC §7.1).

    Numbered one past the highest existing id so it sorts after every gap
    already queued, and re-checked against the ids in use so a hand-edited
    state file cannot produce a collision (two gaps sharing an id would make
    :meth:`Writer.advance_gap` update the wrong one).
    """
    used = set()
    highest = 0
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        gid = gap.get("gap_id")
        if not isinstance(gid, str):
            continue
        used.add(gid)
        match = _GAP_ID_RE.match(gid)
        if match:
            highest = max(highest, int(match.group(1), 10))
    candidate = highest + 1
    while _GAP_ID_FMT % candidate in used:
        candidate += 1
    return _GAP_ID_FMT % candidate


def build_gap(
    gap_id: str,
    start_us: int,
    end_us: int,
    not_before_us: int,
    pass_no: int,
    tag: Optional[str],
) -> Dict[str, Any]:
    """Build one gap record in the exact shape of SPEC §7.1.

    ``cursor_us`` starts at ``start_us`` and is persisted by the backfill worker
    after every successfully written page, so a 429 storm or a reboot resumes
    mid-gap instead of re-walking (SPEC §7.1). ``clamped`` is ``False``: this gap
    was authorised by the operator, so it must not be reported as a misfire of
    the SPEC §7.9 lookback clamp, which flips health to critical.
    """
    return {
        "gap_id": gap_id,
        "start_us": start_us,
        "end_us": end_us,
        "pass": pass_no,
        "attempts": 0,
        "not_before_us": not_before_us,
        "cursor_us": start_us,
        "rows": 0,
        "status": "pending",
        "clamped": False,
        "tag": tag,
    }


def _describe(gap: Dict[str, Any]) -> str:
    """Human-readable one-liner for a gap. Timestamps and ids only."""
    span_us = gap["end_us"] - gap["start_us"]
    return (
        "%s  %s -> %s  (span %.2f days, %d us)  pass=%d not_before=%s tag=%s"
        % (
            gap["gap_id"],
            tsutil.fmt_ts(gap["start_us"]),
            tsutil.fmt_ts(gap["end_us"]),
            span_us / float(_US_PER_DAY),
            span_us,
            gap["pass"],
            tsutil.fmt_ts(gap["not_before_us"]),
            gap["tag"],
        )
    )


def build_parser() -> argparse.ArgumentParser:
    """Command line for manual gap injection. SPEC §11.2."""
    parser = argparse.ArgumentParser(
        prog="python tools/enqueue_gap.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Queue a /chats/window backfill range into state.json by hand\n"
            "(SPEC 11.2). The collector must be stopped."
        ),
        epilog=(
            "WHEN YOU WANT THIS\n"
            "  The collector was down longer than backfill.max_lookback_days\n"
            "  (default 7). Its automatic startup gap is clamped to that window\n"
            "  (SPEC 7.9), so everything older heals only if you ask for it here.\n"
            "  Also useful to re-walk a range you suspect is thin -- backfill is\n"
            "  idempotent, the dedupe key discards every row you already have.\n"
            "\n"
            "PROCEDURE\n"
            "  1. stop the collector      (systemctl stop chat-collector)\n"
            "  2. python tools/enqueue_gap.py <start_iso> <end_iso>\n"
            "  3. start the collector     (systemctl start chat-collector)\n"
            "  4. watch status.json: backfill.gaps_pending falls to 0 and\n"
            "     counters.rows_backfill climbs. At ~1 req/s and 100 rows a page,\n"
            "     a week of chat is roughly 1,300 requests / 20-plus minutes.\n"
            "  The gap is durable the moment this tool returns: the collector\n"
            "  picks it up on its next start, and resumes mid-gap if interrupted.\n"
            "\n"
            "TIMESTAMPS\n"
            "  Any format tsutil.parse_ts accepts. A MISSING ZONE MEANS UTC:\n"
            "  '2026-07-04 09:00:00' is 09:00 UTC, not 09:00 local. Both bounds\n"
            "  are inclusive, matching /chats/window itself (SPEC 7.3), and\n"
            "  overlap is free -- overshoot rather than risk a hole.\n"
            "\n"
            "EXAMPLES\n"
            "  python tools/enqueue_gap.py 2026-06-01T00:00:00Z 2026-06-19T00:00:00Z\n"
            "  python tools/enqueue_gap.py --dry-run 2026-06-01T00:00:00Z \\\n"
            "      2026-06-19T00:00:00Z\n"
            "  python tools/enqueue_gap.py --tag rebuild-june --max-span-days 400 \\\n"
            "      2026-01-01T00:00:00Z 2026-06-19T00:00:00Z\n"
            "\n"
            "EXIT CODES\n"
            "  0 gap queued (or --dry-run validated)\n"
            "  1 the state write failed\n"
            "  2 bad arguments: unparseable instant, zero-or-negative span, or a\n"
            "    span past --max-span-days\n"
            "  3 the collector is running and holds the lock -- stop it first\n"
            "  4 no usable state.json; start the collector once before injecting\n"
        ),
    )
    parser.add_argument("start", metavar="START_ISO", help="inclusive start instant")
    parser.add_argument("end", metavar="END_ISO", help="inclusive end instant")
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="collector.json to read state_path and lock_path from",
    )
    parser.add_argument(
        "--state",
        metavar="PATH",
        default=None,
        help="state.json path, overriding the configured state_path",
    )
    parser.add_argument(
        "--lock",
        metavar="PATH",
        default=None,
        help="lock file path, overriding the configured lock_path",
    )
    parser.add_argument(
        "--tag",
        metavar="NAME",
        default="manual",
        help="provenance tag recorded on the gap (default: manual)",
    )
    parser.add_argument(
        "--pass",
        dest="pass_no",
        type=int,
        default=1,
        metavar="N",
        help="pass number (default 1 = run immediately). Pass 2 is the "
        "confirming re-walk the collector queues itself (SPEC 7.7)",
    )
    parser.add_argument(
        "--not-before",
        metavar="ISO",
        default=None,
        help="do not run the gap before this instant (default: now, i.e. as soon "
        "as the collector starts)",
    )
    parser.add_argument(
        "--max-span-days",
        type=float,
        default=float(DEFAULT_MAX_SPAN_DAYS),
        metavar="D",
        help="refuse spans longer than this (default %d). The guard exists "
        "because a mistyped year is the one input that turns a repair into a "
        "full-history crawl (SPEC 7.9)" % DEFAULT_MAX_SPAN_DAYS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the gap, write nothing",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``python tools/enqueue_gap.py``. SPEC §11.2."""
    args = build_parser().parse_args(argv)
    log = _make_logger()

    try:
        cfg = config.load_config(args.config)
    except config.ConfigError as exc:
        _err(str(exc))
        return EXIT_USAGE
    state_path = args.state or cfg["state_path"]
    lock_path = args.lock or cfg["lock_path"]

    start_us = parse_bound(args.start, "START_ISO")
    end_us = parse_bound(args.end, "END_ISO")
    now_us = tsutil.now_us()

    # SPEC §6.5 / §7.5: startDate == endDate is a 400, and a 400 is a permanent
    # error that alarms and never retries. Refuse before it reaches the queue.
    if end_us <= start_us:
        _err(
            "refusing a zero-or-negative span (%s -> %s): /chats/window returns "
            "HTTP 400 when endDate <= startDate, which the collector treats as a "
            "permanent bug and alarms on (SPEC 6.5, 7.5)"
            % (tsutil.fmt_ts(start_us), tsutil.fmt_ts(end_us))
        )
        return EXIT_USAGE

    span_us = end_us - start_us
    max_span_us = int(args.max_span_days * _US_PER_DAY)
    if max_span_us <= 0:
        _err("--max-span-days must be positive")
        return EXIT_USAGE
    if span_us > max_span_us:
        _err(
            "refusing a %.2f-day span (ceiling %.2f days). Check the year in both "
            "bounds; pass --max-span-days to raise the ceiling deliberately"
            % (span_us / float(_US_PER_DAY), args.max_span_days)
        )
        return EXIT_USAGE
    if args.pass_no < 1:
        _err("--pass must be at least 1")
        return EXIT_USAGE

    not_before_us = (
        parse_bound(args.not_before, "--not-before")
        if args.not_before is not None
        else now_us
    )

    if start_us < _EARLIEST_CHAT_US:
        log.warning(
            "start is before the oldest chat in the database (%s); the window is "
            "legal but the leading part of it is empty",
            tsutil.fmt_ts(_EARLIEST_CHAT_US),
        )
    if end_us > now_us - _RECENT_GUARD_US:
        log.warning(
            "end is inside the last hour of wall clock. Legal today, but if the "
            "maintainer ever enables the commented-out 'migrate to /feed/chats' "
            "guard, a window anchored there returns 400 and the gap goes to "
            "status=error (SPEC 12.14)"
        )
    if end_us > now_us:
        log.warning("end is in the future; the window will simply stop at the newest row")

    # SPEC §10: state.json has exactly one writer. A live collector would
    # overwrite anything written here at its next persist, silently losing the
    # gap, so contention is a hard refusal rather than a warning.
    try:
        handle = singlelock.acquire(lock_path)
    except singlelock.AlreadyRunning as exc:
        _err(
            "the collector is running (lock %s held by pid %s). Stop it first: a "
            "second writer to state.json would lose this gap (SPEC 10). [%s]"
            % (
                lock_path,
                exc.pid if exc.pid is not None else "unknown",
                exc,
            )
        )
        return EXIT_RUNNING
    except OSError as exc:
        _err("cannot open lock file %s (%s)" % (lock_path, type(exc).__name__))
        return EXIT_FAILED

    try:
        try:
            state = statefile.load_state(state_path, log)
        except statefile.StateError as exc:
            _err(str(exc))
            return EXIT_NO_STATE
        if state is None:
            _err(
                "no usable state file at %s. A first run sets hwm_us = now and "
                "does not backfill (SPEC 7.9), so there is nothing to inject "
                "into yet: start the collector once, stop it, then retry"
                % (state_path,)
            )
            return EXIT_NO_STATE

        gaps = state.get("gaps")
        if not isinstance(gaps, list):
            gaps = []
            state["gaps"] = gaps

        gap = build_gap(
            next_gap_id(gaps),
            start_us,
            end_us,
            not_before_us,
            args.pass_no,
            args.tag,
        )

        if args.dry_run:
            sys.stdout.write("would queue: %s\n" % (_describe(gap),))
            sys.stdout.write(
                "state %s left untouched (%d gap(s) currently queued)\n"
                % (state_path, len(gaps))
            )
            return EXIT_OK

        gaps.append(gap)
        try:
            statefile.save_state(state_path, state)
        except (statefile.StateError, OSError) as exc:
            _err("could not write %s (%s)" % (state_path, type(exc).__name__))
            return EXIT_FAILED

        pending = sum(
            1
            for g in gaps
            if isinstance(g, dict) and g.get("status") in ("pending", "running")
        )
        sys.stdout.write("queued: %s\n" % (_describe(gap),))
        sys.stdout.write(
            "state %s now holds %d gap(s), %d runnable. Start the collector to "
            "run it.\n" % (state_path, len(gaps), pending)
        )
        return EXIT_OK
    finally:
        handle.release()


if __name__ == "__main__":
    sys.exit(main())
