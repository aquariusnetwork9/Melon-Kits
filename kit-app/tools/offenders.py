"""Rank accounts in the bulk chat dump by the categories that actually change a decision.

    python tools/offenders.py --chats chats.csv --lexicon lexicon.json --out offenders.tsv

Produces a candidate list for `/flag <name> deny` -- the reviewer-maintained do-not-serve list.
It is a **candidate** list and nothing more: a keyword matcher cannot see context, so every name
it produces still needs a human to read the lines before anything is written down. See
docs/reviewing.md for why that separation is the whole design.

**Ranks on `slur` and `off_game`, deliberately not on profanity.** Measured against this same
dump, the tuned lexicon matches 3.7% of *all* 2b2t chat -- profanity is the server's ambient
register, so ranking by it ranks by who talks most and nothing else. `off_game` is weighted
hardest because it is the one category that leaves the game: doxxing, swatting, real-world
threats target a person rather than a player. `scam` is reported but not ranked on, since it is
a different question from abuse.

**Aggregates by UUID, not by name.** A rename would otherwise split one person's history into
two innocent-looking halves, and 2b2t players rename often. The name shown is the one they used
most in the window.

**Volume is reported next to every count**, because 40 hits in 60,000 lines and 40 hits in 200
lines are completely different claims and the ranking alone cannot express that.

Output is a TSV **file**, never stdout: it is a list of accusations, and a terminal is a worse
place for that than a file you control. It contains names and counts and never chat text -- if
you want to read what somebody said, that is `mine.py test`, or the bot's own chat pager.

Standard library only, streams the CSV, safe against the full 6.7M rows (~15 minutes).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import screening        # noqa: E402

csv.field_size_limit(1 << 24)

# What each category contributes to the rank. off_game outweighs slur because it is the one
# category whose own lexicon note says it should reliably change a decision, and because it is
# rare enough that a single genuine hit is worth a reviewer's time. profanity is 0.0 on purpose:
# see the module docstring. scam is counted and shown but stays out of the ranking.
WEIGHTS = {"off_game": 10.0, "slur": 3.0, "scam": 0.0, "profanity": 0.0}


def open_chats(path):
    fh = open(path, "r", encoding="utf-8", errors="replace", newline="")
    reader = csv.reader(fh)
    header = next(reader, None)
    if header is None:
        raise SystemExit("%s is empty" % path)
    lowered = [h.strip().lower() for h in header]

    def find(*names):
        for name in names:
            if name in lowered:
                return lowered.index(name)
        return None

    chat = find("chat", "message", "text", "content")
    name = find("player_name", "playername", "name")
    uuid = find("player_uuid", "playeruuid", "uuid")
    if chat is None or (name is None and uuid is None):
        raise SystemExit("need a chat column and a player column; header is %s" % header)
    return fh, reader, chat, name, uuid


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chats", required=True)
    ap.add_argument("--lexicon", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=50,
                    help="how many accounts to write (default 50)")
    ap.add_argument("--min-lines", type=int, default=1,
                    help="ignore accounts with fewer than this many lines in the window")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (trial run)")
    args = ap.parse_args(argv)

    lex = screening.Lexicon.load(args.lexicon)
    if not lex:
        raise SystemExit("lexicon %s has no terms" % args.lexicon)
    ranked_cats = sorted(c for c in lex.categories if WEIGHTS.get(c, 0.0) > 0)
    if not ranked_cats:
        raise SystemExit(
            "none of the lexicon's categories (%s) carry weight, so there is nothing to rank. "
            "Ranking on profanity ranks by who talks most." % ", ".join(sorted(lex.categories)))
    sys.stderr.write("lexicon: %d terms, ranking on %s\n"
                     % (lex.term_count, " + ".join(ranked_cats)))

    fh, reader, c_chat, c_name, c_uuid = open_chats(args.chats)
    lines = Counter()                                   # key -> lines said
    hits = defaultdict(Counter)                         # key -> category -> hits
    flagged = defaultdict(Counter)                       # key -> category -> lines involved
    names = defaultdict(Counter)                        # key -> name -> times seen
    started = time.time()
    rows = 0
    try:
        for row in reader:
            if len(row) <= c_chat:
                continue
            rows += 1
            if args.limit and rows > args.limit:
                break
            if rows % 500000 == 0:
                rate = rows / max(1e-6, time.time() - started)
                sys.stderr.write("\r  %s rows  (%s/s)  %s accounts"
                                 % (f"{rows:,}", f"{int(rate):,}", f"{len(lines):,}"))
                sys.stderr.flush()

            key = (row[c_uuid].strip() if c_uuid is not None and len(row) > c_uuid else "") \
                or (row[c_name].strip() if c_name is not None and len(row) > c_name else "")
            if not key:
                continue
            lines[key] += 1
            if c_name is not None and len(row) > c_name and row[c_name].strip():
                names[key][row[c_name].strip()] += 1

            text = row[c_chat]
            if not text:
                continue
            found = screening.scan(text, lex)
            if not found:
                continue
            seen_cats = set()
            for hit in found:
                hits[key][hit.category] += 1
                seen_cats.add(hit.category)
            for cat in seen_cats:
                flagged[key][cat] += 1
    finally:
        fh.close()
    sys.stderr.write("\n")

    def score(key):
        return sum(WEIGHTS.get(cat, 0.0) * n for cat, n in hits[key].items())

    candidates = [k for k in hits
                  if score(k) > 0 and lines[k] >= max(1, args.min_lines)]
    candidates.sort(key=lambda k: (-score(k), -sum(hits[k].values()), k))

    cats = sorted(lex.categories)
    with open(args.out, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out, delimiter="\t", lineterminator="\n")
        w.writerow(["rank", "name", "uuid", "lines_in_window", "weighted_score"]
                   + ["%s_hits" % c for c in cats]
                   + ["%s_lines" % c for c in cats]
                   + ["ranked_lines", "pct_of_their_lines"])
        for i, key in enumerate(candidates[:args.top], 1):
            name = names[key].most_common(1)[0][0] if names[key] else "(unknown)"
            ranked_lines = sum(flagged[key][c] for c in ranked_cats)
            w.writerow(
                [i, name, key if key != name else "", lines[key], "%.1f" % score(key)]
                + [hits[key].get(c, 0) for c in cats]
                + [flagged[key].get(c, 0) for c in cats]
                + [ranked_lines, "%.2f%%" % (100.0 * ranked_lines / max(1, lines[key]))])

    sys.stderr.write(
        "%s rows, %s accounts seen, %s with a ranked hit -> %s\n"
        % (f"{rows:,}", f"{len(lines):,}", f"{len(candidates):,}", args.out))
    sys.stderr.write(
        "This is a CANDIDATE list. A keyword matcher cannot see context, so read the lines "
        "before flagging anybody.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
