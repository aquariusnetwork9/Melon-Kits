"""Mine the 2b2t bulk chat dump for the spellings people actually use.

    python tools/mine.py discover --chats chats.csv --out tokens.tsv --top 4000
    python tools/mine.py test     --chats chats.csv --lexicon lexicon.json --out report.txt

**This deliberately reuses `screening.normalise` and `screening.scan`.** Mining with any
other folding -- a DuckDB `lower()`, a hand-rolled regex -- produces variants that look right
and then fail to match at runtime, because the bot folds differently. One normaliser, used by
both, or the exercise is worthless.

Two modes, and they answer different questions.

**discover** builds a frequency table of normalised word-tokens together with the original
spellings that produced each one. This is the ground truth a lexicon is written from: you are
looking for the forms that are *common*, and reading back what they actually looked like.
Nobody guesses the top twenty variants of a word correctly, so do not try.

**test** runs a candidate lexicon over the whole corpus and reports, per term: how many lines
it hits, the base rate per 1,000 lines, every original spelling ranked by frequency, and a
sample of matched lines so you can see what it is really catching. A term that fires on 4% of
all chat is not evidence about an individual; a term whose sample is full of innocent
collocations needs an `exceptions` entry.

Output goes to a **file**, never stdout. The point of the exercise is a list of slurs and
threats, and a terminal is a worse place for that than a file you control. Sample lines are
coordinate-redacted first, because the dump is real chat and the coordinates in it are real
people's bases.

Standard library only, streams the CSV, and safe to run against the full 6.7M rows.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import redact           # noqa: E402
import screening        # noqa: E402

# A chat line can be long; the default field cap is smaller than some of them.
csv.field_size_limit(1 << 24)

# Split the ORIGINAL line into word-ish tokens *before* normalising each one. Normalising a
# whole line deliberately strips separators -- that is what defeats "b a d w o r d" -- but it
# also fuses neighbouring words, so it is the wrong primitive for a frequency table.
_TOKEN = re.compile(r"[^\s]+")

# Cap distinct spellings retained per normalised form. Bounds memory on a 6.7M-row pass
# without losing anything interesting: the tail past 50 variants is noise.
_MAX_SPELLINGS = 50


def open_chats(path):
    fh = open(path, "r", encoding="utf-8", errors="replace", newline="")
    reader = csv.reader(fh)
    header = next(reader, None)
    if header is None:
        raise SystemExit("%s is empty" % path)
    lowered = [h.strip().lower() for h in header]
    for want in ("chat", "message", "text", "content"):
        if want in lowered:
            return fh, reader, lowered.index(want), header
    raise SystemExit("no chat column in %s; header is %s" % (path, header))


def progress(n, started, every=500000):
    if n % every:
        return
    rate = n / max(1e-6, time.time() - started)
    sys.stderr.write("\r  %s rows  (%s/s)" % (f"{n:,}", f"{int(rate):,}"))
    sys.stderr.flush()


def cmd_discover(args):
    fh, reader, col, _ = open_chats(args.chats)
    counts: Counter = Counter()
    spellings = {}
    started, n = time.time(), 0
    with fh:
        for row in reader:
            if len(row) <= col:
                continue
            line = row[col]
            if not line:
                continue
            n += 1
            if args.limit and n > args.limit:
                break
            progress(n, started)
            for raw in _TOKEN.findall(line):
                norm, _ = screening.normalise(raw)
                if len(norm) < args.min_len:
                    continue
                counts[norm] += 1
                bucket = spellings.setdefault(norm, Counter())
                if raw in bucket or len(bucket) < _MAX_SPELLINGS:
                    bucket[raw] += 1
    sys.stderr.write("\r  %s rows scanned, %s distinct normalised tokens\n"
                     % (f"{n:,}", f"{len(counts):,}"))

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("# normalised token frequency from %s (%d lines)\n" % (args.chats, n))
        out.write("# columns: count<TAB>normalised<TAB>top original spellings\n")
        out.write("# Pick the forms that matter to you; put the NORMALISED form in the\n")
        out.write("# lexicon -- one entry covers its whole spelling family.\n")
        for norm, c in counts.most_common(args.top):
            variants = ", ".join("%s(%d)" % (w, k)
                                 for w, k in spellings[norm].most_common(8))
            out.write("%d\t%s\t%s\n" % (c, norm, variants))
    print("wrote %s (top %d of %s tokens)" % (args.out, args.top, f"{len(counts):,}"))
    return 0


def cmd_test(args):
    lex = screening.Lexicon.load(args.lexicon)
    if not lex:
        raise SystemExit("%s has no terms in it yet" % args.lexicon)
    fh, reader, col, _ = open_chats(args.chats)

    per_cat: Counter = Counter()
    per_term: Counter = Counter()
    term_lines: Counter = Counter()
    spellings = {}
    samples = {}
    started, n, flagged = time.time(), 0, 0

    with fh:
        for row in reader:
            if len(row) <= col:
                continue
            line = row[col]
            if not line:
                continue
            n += 1
            if args.limit and n > args.limit:
                break
            progress(n, started)
            hits = screening.scan(line, lex)
            if not hits:
                continue
            flagged += 1
            for term in {h.term for h in hits}:
                term_lines[term] += 1
                if len(samples.setdefault(term, [])) < args.samples:
                    # Redacted: the dump is real chat and the coordinates in it are real
                    # people's bases. A false-positive audit does not need them.
                    samples[term].append(redact.redact(line)[0][:200])
            for h in hits:
                per_cat[h.category] += 1
                per_term[h.term] += 1
                spellings.setdefault(h.term, Counter())[h.spelling.casefold()] += 1

    sys.stderr.write("\r  %s rows scanned\n" % f"{n:,}")

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("LEXICON REPORT  %s over %s\n" % (args.lexicon, args.chats))
        out.write("=" * 78 + "\n")
        out.write("lines scanned : %s\n" % f"{n:,}")
        out.write("lines flagged : %s  (%.3f%% of all chat)\n"
                  % (f"{flagged:,}", 100.0 * flagged / max(1, n)))
        out.write("\nBY CATEGORY\n")
        for cat, c in per_cat.most_common():
            out.write("  %-22s %s hits\n" % (cat, f"{c:,}"))

        out.write("\nBY TERM  (per-1k is lines-per-1000, the number that says whether a\n")
        out.write("          term is evidence about a person or just ambient)\n")
        for term, c in per_term.most_common():
            lines = term_lines[term]
            out.write("\n  %-24s %s hits, %s lines, %.3f per 1k\n"
                      % (term, f"{c:,}", f"{lines:,}", 1000.0 * lines / max(1, n)))
            variants = spellings[term].most_common(25)
            out.write("    spellings: %s\n"
                      % ", ".join("%s(%d)" % (w, k) for w, k in variants))
            if len(spellings[term]) > 25:
                out.write("    ...and %d more distinct spellings\n"
                          % (len(spellings[term]) - 25))
            out.write("    sample lines (coordinate-redacted):\n")
            for s in samples.get(term, []):
                out.write("      | %s\n" % s)

        out.write("\n" + "=" * 78 + "\n")
        out.write("READING THIS\n")
        out.write("  - A high per-1k means the term is ambient register, not a signal about\n")
        out.write("    an individual. Profanity will look like this. That is expected.\n")
        out.write("  - Sample lines full of innocent uses mean the term needs an\n")
        out.write("    'exceptions' entry -- the innocent word is blanked before matching,\n")
        out.write("    so 'grapefruit' can suppress 'grape' cleanly.\n")
        out.write("  - Spellings you did not predict are the whole point. You do not need to\n")
        out.write("    add them: the normaliser already folds them onto the same form.\n")
    print("wrote %s" % args.out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="frequency table of normalised tokens")
    d.add_argument("--chats", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--top", type=int, default=4000)
    d.add_argument("--min-len", type=int, default=3)
    d.add_argument("--limit", type=int, default=0, help="stop after N rows (for a trial run)")
    d.set_defaults(func=cmd_discover)

    t = sub.add_parser("test", help="run a candidate lexicon over the corpus")
    t.add_argument("--chats", required=True)
    t.add_argument("--lexicon", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--samples", type=int, default=6)
    t.add_argument("--limit", type=int, default=0)
    t.set_defaults(func=cmd_test)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
