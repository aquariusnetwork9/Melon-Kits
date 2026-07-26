"""Strip coordinate-like values out of chat before anything displays it.

2b2t public chat routinely carries coordinates, and they are usually **someone else's** --
a base leaked in an argument, a stash traded in public, a location someone is being hunted
to. The applicant consented to their chat being reviewed; the third party in their chat log
did not, and a Discord thread is a durable, searchable, screenshot-able place. So this runs
on every line before it reaches an embed, a file or a reviewer.

The design bias is deliberate: **over-redact rather than under-redact.** A reviewer reading
``[coords]`` where a bare pair of large numbers used to be loses nothing that matters to the
decision they are making; a leaked base costs someone their base. False positives are
therefore accepted on purpose, and the substitution is counted so the card can say how many
values were hidden rather than pretending the line was always that way.

What is *not* redacted, on purpose:

- A lone large number. Counts, years, prices and stack sizes all look like this and
  redacting them would shred readability for no gain -- one number is not a location.
- A single labelled axis with no sibling (``x: 1234`` alone). Ambiguous, and a single axis
  does not locate anything.

Heuristics, not a parser. New evasion spellings are expected; the corpus is how you find
them (see docs/chat-screening.md), and `find_coords` exists so a candidate pattern can be
audited against the 2025 dump before it ships.
"""
from __future__ import annotations

import re
from typing import List, Sequence, Tuple

PLACEHOLDER = "[coords]"

# A coordinate component: optional sign, up to 7 digits, an optional decimal tail, an
# optional k/m magnitude suffix. Deliberately does NOT treat '.' as a separator, so a
# version string like 1.21.4 parses as 1.21 and 4 -- neither of which is "large" -- and
# survives untouched.
_NUM = r"[-+]?\d{1,7}(?:\.\d{1,3})?\s?[kKmM]?"

# Separators seen between coordinates in real chat. No '.', see above.
_SEP = r"[\s,;:/|]+"

_TRIPLE = re.compile(r"(?<![\w.])(%s)%s(%s)%s(%s)(?![\w])" % (_NUM, _SEP, _NUM, _SEP, _NUM))
_PAIR = re.compile(r"(?<![\w.])(%s)%s(%s)(?![\w])" % (_NUM, _SEP, _NUM))
_AXIS = re.compile(r"(?<![\w])([xyzXYZ])\s*[:=]?\s*(%s)(?![\w])" % (_NUM,))

_LARGE = 100.0


def _value(text: str) -> Tuple[float, bool]:
    """Parse a component. Returns (magnitude, had_suffix)."""
    body = text.strip().replace(" ", "")
    mult, suffix = 1.0, False
    if body and body[-1] in "kKmM":
        suffix = True
        mult = 1000.0 if body[-1] in "kK" else 1_000_000.0
        body = body[:-1]
    try:
        return abs(float(body)) * mult, suffix
    except ValueError:
        return 0.0, suffix


def _is_large(text: str) -> bool:
    """A component big enough to plausibly be a world coordinate.

    A k/m suffix alone qualifies: '2k' is 2000 whichever way you read it, and nobody writes
    a stack size that way.
    """
    mag, suffix = _value(text)
    return suffix or mag >= _LARGE


def redact(text: str) -> Tuple[str, int]:
    """Return (redacted_text, number_of_substitutions)."""
    if not text:
        return text, 0
    count = 0

    def triple(m: "re.Match") -> str:
        # The middle value is free: a y-level is 0..320 and often single-digit, so demanding
        # all three be large would miss the single most common shape there is.
        nonlocal count
        if _is_large(m.group(1)) and _is_large(m.group(3)):
            count += 1
            return PLACEHOLDER
        return m.group(0)

    def pair(m: "re.Match") -> str:
        nonlocal count
        if _is_large(m.group(1)) and _is_large(m.group(2)):
            count += 1
            return PLACEHOLDER
        return m.group(0)

    out = _TRIPLE.sub(triple, text)
    # Runs the pair pass after the triple pass: PLACEHOLDER holds no digits, so an already
    # redacted span cannot be partially re-matched into a neighbour's number.
    out = _PAIR.sub(pair, out)

    # Labelled axes last, and only when two or more distinct axes are present -- 'x: 1234'
    # on its own locates nothing, while 'x 1234 z -5678' is unmistakable even though the
    # letters break up the numeric runs the passes above look for.
    axes = _AXIS.findall(out)
    if len({a.lower() for a, _ in axes}) >= 2:
        def axis(m: "re.Match") -> str:
            nonlocal count
            count += 1
            return PLACEHOLDER
        out = _AXIS.sub(axis, out)

    return out, count


def redact_lines(lines: Sequence[str]) -> Tuple[List[str], int]:
    """Redact a batch, returning the total substitution count across all of them."""
    out, total = [], 0
    for line in lines:
        clean, n = redact(line)
        out.append(clean)
        total += n
    return out, total


def find_coords(text: str) -> List[str]:
    """The spans `redact` would replace. For auditing a pattern against the bulk dump.

    Never call this on a path that logs or displays its result -- the whole point of the
    return value is that it *is* the coordinates.
    """
    found = []
    for m in _TRIPLE.finditer(text):
        if _is_large(m.group(1)) and _is_large(m.group(3)):
            found.append(m.group(0))
    scrubbed = _TRIPLE.sub(lambda m: PLACEHOLDER if (
        _is_large(m.group(1)) and _is_large(m.group(3))) else m.group(0), text)
    for m in _PAIR.finditer(scrubbed):
        if _is_large(m.group(1)) and _is_large(m.group(2)):
            found.append(m.group(0))
    return found


def shape(text: str, limit: int = 80) -> str:
    """A loggable silhouette of a line: letters to 'a', digits to '0', spacing kept.

    For the rare diagnostic that needs to say *something* about a line it could not handle.
    Chat content, coordinates included, must never reach a log; a shape carries the
    structure needed to fix a parser and none of the content.
    """
    out = []
    for ch in text[:limit]:
        if ch.isdigit():
            out.append("0")
        elif ch.isalpha():
            out.append("a")
        elif ch.isspace():
            out.append(" ")
        else:
            out.append(ch)
    tail = "..." if len(text) > limit else ""
    return "".join(out) + tail
