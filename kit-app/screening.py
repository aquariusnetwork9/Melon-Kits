"""Keyword counting over chat, with the normalisation that makes a lexicon worth having.

This produces **counts and matched categories. Never a score, never a verdict.** That is a
design constraint, not a limitation: profanity is 2b2t's ambient register, so a number
attached to it would immediately be read as the answer to a question it cannot answer. The
reviewer reads the lines; the counts only decide what order to read them in. See
docs/reviewing.md.

The value here is the normaliser, not the matcher. Filter evasion is an orthography problem
-- people write around a word rather than avoiding it -- so matching the literal spelling of
a term catches almost nothing. Folding case, homoglyphs, digit substitutions, separators and
repeated characters collapses the whole family of spellings onto one form.

`normalise` keeps an index back to the original text so a match can report **the spelling
that actually appeared**. That is the input to the mining loop in docs/chat-screening.md:
normalise a corpus, group by normalised form, then read back the original spellings ranked
by frequency. Nobody guesses the top twenty variants correctly, so don't try -- harvest them.

Exceptions are applied by deleting them from the normalised text *before* matching, which is
what makes the Scunthorpe family of collisions go away cleanly: the innocent word is gone by
the time anything looks for a substring inside it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

# Substitutions people actually use, folded onto letters. Deliberately lossy and
# many-to-one: the goal is to collapse a family of spellings, not to round-trip.
_FOLD = {
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b",
    "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "£": "l", "€": "e", "+": "t",
    "(": "c", "<": "c", "¡": "i", "¿": "?",
}


class Lexicon(object):
    """Categories of terms to count. Load with `Lexicon.load`, or build empty.

    Shape on disk::

        {"categories": {
            "off_game_threat": {
                "note": "leaves the game -- doxxing, swatting, real-world threats",
                "terms": ["doxx", "swat you", "ip grab"],
                "exceptions": []}}}

    There is no weight field on purpose. A weight is a score with extra steps.
    """

    def __init__(self, categories: Optional[Dict[str, dict]] = None) -> None:
        self.categories: Dict[str, dict] = {}
        for name, body in (categories or {}).items():
            terms = [t for t in (body.get("terms") or []) if isinstance(t, str) and t.strip()]
            excs = [t for t in (body.get("exceptions") or []) if isinstance(t, str) and t.strip()]
            self.categories[name] = {
                "note": body.get("note") or "",
                # Store normalised, so matching never re-normalises the lexicon per line.
                "terms": sorted({normalise(t)[0] for t in terms} - {""}),
                "exceptions": sorted({normalise(t)[0] for t in excs} - {""}),
                "raw_terms": terms,
            }

    @classmethod
    def load(cls, path: Optional[str]) -> "Lexicon":
        if not path:
            return cls({})
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("categories"), dict):
            raise ValueError("lexicon must be an object with a 'categories' object")
        return cls(doc["categories"])

    def __bool__(self) -> bool:
        return any(c["terms"] for c in self.categories.values())

    @property
    def term_count(self) -> int:
        return sum(len(c["terms"]) for c in self.categories.values())


def normalise(text: str) -> Tuple[str, List[int]]:
    """Fold `text` to a matchable form. Returns (normalised, index_map).

    ``index_map[i]`` is the offset in `text` that produced ``normalised[i]``, so a match
    span maps back to the spelling that really appeared.

    Steps: NFKD and drop combining marks (so ``é`` and ``e`` agree, and zalgo collapses),
    casefold, fold homoglyphs and digits onto letters, drop everything non-alphanumeric,
    then collapse runs of a repeated character to one (``niiiice`` -> ``nice``).
    """
    out: List[str] = []
    index: List[int] = []
    decomposed: List[Tuple[str, int]] = []

    for pos, ch in enumerate(text):
        for piece in unicodedata.normalize("NFKD", ch):
            if not unicodedata.combining(piece):
                decomposed.append((piece, pos))

    for piece, pos in decomposed:
        low = piece.casefold()
        folded = _FOLD.get(low, low)
        if not folded.isalnum():
            continue
        for sub in folded:
            # Collapse a run of the same character. Done here rather than by regex so the
            # index map stays aligned with what survived.
            if out and out[-1] == sub:
                continue
            out.append(sub)
            index.append(pos)

    return "".join(out), index


class Match(object):
    __slots__ = ("category", "term", "spelling", "start", "end")

    def __init__(self, category: str, term: str, spelling: str, start: int, end: int) -> None:
        self.category = category
        self.term = term
        self.spelling = spelling      # as it appeared in the original line
        self.start = start
        self.end = end

    def __repr__(self) -> str:                     # pragma: no cover - debugging aid
        return "Match(%s, %r)" % (self.category, self.spelling)


# A term shorter than this only ever matches a whole token. Longer ones may match inside one,
# so "fuckyou" is caught by "fuck" while "as" cannot fire inside "was".
_MIN_INFIX = 6

# A token is a run of alphanumerics *or leet substitutes*. Splitting on whitespace alone is
# not enough: `gg/ok` would stay one token, and it folds to exactly the same string as a
# slur, which produced 22,245 false hits in the 2025 corpus. Punctuation therefore ends a
# token -- but the symbols that stand in for letters (`@`, `$`, `!`, `|`, ...) must not, or
# `b@dword` would be torn in half. Underscore splits too, which is what we want for names.
_LEET_CHARS = "".join(re.escape(k) for k in _FOLD if not k.isalnum())
_WORD = re.compile(r"(?:[^\W_]|[" + _LEET_CHARS + r"])+")


def _blank(haystack: str, exceptions: List[str]) -> str:
    """Replace exception terms with spaces, preserving every offset.

    Spaces rather than deletion because the index map has to keep lining up, and ' ' is
    non-alphanumeric so it can never become part of a term.
    """
    for exc in exceptions:
        if exc:
            haystack = haystack.replace(exc, " " * len(exc))
    return haystack


def scan(text: str, lex: Lexicon) -> List[Match]:
    """Every lexicon term found in one line, with the original spelling of each hit.

    Matching is deliberately NOT a plain substring search of the normalised line, and the
    reason is measurable: run that against the 2025 bulk dump and it flags **26% of all
    2b2t chat**. Normalisation collapses repeated letters, so `ass` -> `as`, `hell` -> `hel`,
    `coon` -> `con`; a substring search then fires those inside `was`, `hello` and `control`.
    `as` alone hit 1.18 million lines. A screen that flags a quarter of everything tells a
    reviewer nothing.

    So a term matches only when it is one of:

    1. **A whole normalised token.** `badword` matches the word, however it was spelled --
       `B4DW0RD`, `BaAaDword`, `bádword` all normalise to the same token.
    2. **Inside a token, if the term is at least 6 characters.** Catches compounds like
       `fuckyou` without letting three-letter terms fire inside ordinary words.
    3. **Spanning two or more tokens.** This is the separator-evasion case -- `b a d w o r d`,
       `b.a.d.w.o.r.d` -- and it is only accepted when the original span actually crosses a
       separator, which no innocent single word can do.

    Rule 3 is what keeps the evasion coverage that made the whole-line search attractive,
    without the collateral damage.
    """
    if not text or not lex:
        return []
    norm, index = normalise(text)
    if not norm:
        return []

    tokens = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    token_norm = [normalise(text[a:b]) for a, b in tokens]

    out: List[Match] = []
    seen = set()

    def add(cat, term, o_start, o_end):
        span = text[o_start:o_end]
        # Reject a match that is mostly digits. Leet substitution is *letters written as
        # digits*, so the result still reads as a word with a minority of digits -- a
        # majority-digit string is a number.
        #
        # This is not defensive tidying, it is the single biggest false-positive source
        # measured against the bulk dump. On a Minecraft server people talk in distances and
        # coordinates constantly, and the fold maps 6->g, 0->o, 4->a, 8->b, so `60k` and
        # `600k` normalise to `gok` and `480` normalises to `abo`. In the 2025 corpus that
        # was tens of thousands of false hits against 18 genuine ones.
        digits = sum(ch.isdigit() for ch in span)
        letters = sum(ch.isalpha() for ch in span)
        if digits > letters:
            return
        key = (cat, term, o_start, o_end)
        if key in seen:
            return
        seen.add(key)
        out.append(Match(cat, term, span, o_start, o_end))

    for cat, body in sorted(lex.categories.items()):
        terms = body["terms"]
        if not terms:
            continue
        excs = body["exceptions"]

        # --- rules 1 and 2: within a single token ---------------------------
        for (o_start, o_end), (tnorm, tindex) in zip(tokens, token_norm):
            if not tnorm:
                continue
            hay = _blank(tnorm, excs)
            for term in terms:
                if hay == term:
                    add(cat, term, o_start, o_end)
                elif len(term) >= _MIN_INFIX:
                    at = hay.find(term)
                    while at != -1:
                        s = o_start + tindex[at]
                        e = o_start + tindex[min(at + len(term), len(tindex)) - 1] + 1
                        add(cat, term, s, e)
                        at = hay.find(term, at + 1)

        # --- rule 3: spanning tokens, i.e. deliberate separator evasion -----
        # Same length floor as the infix rule, and for the same measured reason. A short
        # term will find its letters lying consecutively across a word boundary constantly:
        # in the 2025 corpus `gok` matched `gg/ok`-shaped spans 22,245 times, `abo` matched
        # `a bo` 15,439 times, `hel` matched `he l` 31,273 times. Spelling a word out with
        # separators is only a recognisable evasion when the word is long enough that doing
        # so is deliberate; at three letters it is just prose.
        line_hay = _blank(norm, excs)
        for term in terms:
            if len(term) < _MIN_INFIX:
                continue
            at = line_hay.find(term)
            while at != -1:
                o_start = index[at]
                o_end = index[min(at + len(term), len(index)) - 1] + 1
                span = text[o_start:o_end]
                # Only interesting if the letters came from more than one token. A match
                # inside a single word was already judged by rules 1 and 2.
                if any(not ch.isalnum() for ch in span):
                    add(cat, term, o_start, o_end)
                at = line_hay.find(term, at + 1)

    return out


def summarise(lines: Sequence[str], lex: Lexicon) -> Dict[str, object]:
    """Per-category counts across a batch, plus which lines to read first.

    ``lines_flagged`` counts *lines*, not hits: one person saying one word forty times is a
    different thing from forty people saying it once, and the count that answers the
    reviewer's question is how much of the chat is involved.
    """
    per_category: Dict[str, int] = {}
    lines_flagged = 0
    spellings: Dict[str, int] = {}
    hot: List[int] = []

    for i, line in enumerate(lines):
        hits = scan(line, lex)
        if not hits:
            continue
        lines_flagged += 1
        hot.append(i)
        for hit in hits:
            per_category[hit.category] = per_category.get(hit.category, 0) + 1
            key = hit.spelling.casefold()
            spellings[key] = spellings.get(key, 0) + 1

    return {
        "lines_scanned": len(lines),
        "lines_flagged": lines_flagged,
        "per_category": per_category,
        # Original spellings by frequency -- the mining output, and what tells you a term
        # needs a variant added to the lexicon.
        "spellings": sorted(spellings.items(), key=lambda kv: (-kv[1], kv[0])),
        "flagged_line_indexes": hot,
    }
