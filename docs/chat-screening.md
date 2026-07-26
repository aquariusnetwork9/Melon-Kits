# Chat screening

A keyword list with counts, shown beside the applicant's recent chat. That is the whole
feature. This document is mostly about why it is not more than that, and how to make the list
good.

---

## Why not a model

The 2025 bulk dump and the live corpus are both **unlabelled**. Supervised training would mean
days of hand-labelling before producing anything, to beat a keyword list at a task where a
human is already reading the lines.

Weak supervision doesn't rescue it either: seeded from your own lexicon, it mostly relearns
your lexicon and hands the result back with a confidence score attached, which is worse than
useless because it looks authoritative.

Inference cost was never the obstacle — it is under a dollar in the worst case. The obstacles
are wall-clock against a
[globally shared rate limit](external-apis.md#the-rate-limit-is-global-not-per-ip) and the
absence of a labelled golden set to know whether anything you built actually works.

And the ceiling is falling: **2b2t added its own chat filter**, so the amount of screenable
material shrinks from here. Build it cheap and stop.

## What the corpus is actually for

**Mining, not training.** The high-value query is:

1. Normalise the corpus — case-fold, strip separators, collapse repeats, map homoglyphs and
   digit substitutions.
2. Group by the normalised form.
3. Look back at the **original spellings**, ranked by frequency.

That hands you the evasion spellings people really type, which reliably beats the leetspeak
rules you would have invented. Nobody guesses the top twenty variants correctly.

Also worth an afternoon:

- **Collocations** — the multi-word phrases that matter and that single-token matching cannot
  see.
- **False-positive audit at scale** — run a candidate pattern across 6.7M lines and read what
  it catches. Scunthorpe-class collisions are cheap to find here and expensive to find in
  production.
- **Base rates** — how common is a term in ordinary 2b2t chat. A term that appears in 4% of
  all lines is not evidence about an individual.
- **Fuzzy-match thresholds set from data** rather than from taste.

All of it is DuckDB against the CSVs directly. Convert to Parquet if you'll iterate.

This is where the corpus earns its keep, and it explains why the live collector runs
continuously: the filter changes *what* gets said, but the mining target is **how people spell
things when they are evading a filter** — orthography, not content. A server-side filter
plausibly produces *more* creative evasion, so that value holds even as the content thins out.

---

## Shape of the output

Counts and matched categories. Never a score, never a verdict, never a recommendation.

The reviewer reads the actual lines
([and here is why](reviewing.md#recent-public-chat)); the counts only decide what order to
read them in. A screening feature that produces a number invites the number to be used as the
answer, which is precisely the failure this design is avoiding.

---

## Labels arrive free, but only if you ask on day one

Log which lines were shown, which the reviewer flagged, and every decision with its reason.
See [kit-request-flow.md](kit-request-flow.md#instrument-this-from-day-one) — this is the one
thing in the project that is impossible to add retroactively.
