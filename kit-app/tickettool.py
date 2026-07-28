"""Reading Ticket Tool transcripts.

Stdlib only, like every module here except `bot.py`, so the parsing can be tested without
discord.py installed and without a mocked gateway. `deploy/import_tickettool.py` is the thin
Discord shell around this.

The format, established by inspecting real transcripts rather than documentation: Ticket Tool
attaches an HTML file whose visible body is only a header. The conversation is a base64 JSON
blob assigned to `let messages = "..."`, which the page decodes client-side. So a transcript is
fully readable from the Discord attachment alone -- nothing has to be fetched from tickettool.xyz.

The Minecraft name is not a structured field. Ticket Tool's opening embed asks people to state
it in prose, so it arrives as free text and has to be recovered by pattern. That is only safe
because a candidate can afterwards be checked against Mojang: the cost of a wrong guess is a
stranger inheriting somebody else's kit history.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

# How people actually write it, taken from real tickets rather than invented. Ordered most
# explicit first. `[\w.]{3,16}` is Minecraft's own name shape, which rejects most prose on its
# own -- and whatever survives still has to resolve against Mojang.
NAME_PATTERNS = [
    re.compile(r"(?:minecraft|mc)\s*(?:user\s*name|username|name|ign)\s*(?:is|:|=)?\s*[\"'`]?([\w.]{3,16})", re.I),
    re.compile(r"in[\s-]*game\s*(?:name|tag)\s*(?:is|:|=)?\s*[\"'`]?([\w.]{3,16})", re.I),
    re.compile(r"\bign\s*(?:is|:|=)?\s*[\"'`]?([\w.]{3,16})", re.I),
    re.compile(r"\bigt\s*(?:is|:|=)?\s*[\"'`]?([\w.]{3,16})", re.I),
    re.compile(r"\busername\s*(?:is|:|=)\s*[\"'`]?([\w.]{3,16})", re.I),
    re.compile(r"\bmy\s+name\s+(?:is|:)\s*[\"'`]?([\w.]{3,16})", re.I),
]

# Shapes that match but are never an account. Kept deliberately small: the Mojang check is the
# real filter, and a long stopword list would start rejecting genuine names -- people are
# called "Melon" and "Player" on 2b2t.
STOPWORDS = {
    "the", "and", "but", "for", "you", "your", "yours", "please", "thanks", "thank",
    "hello", "hey", "help", "kit", "kits", "spawn", "queue", "priority",
    "minecraft", "discord", "same", "above", "below", "there", "here", "that", "this",
    "not", "yet", "still", "just", "about", "from", "with", "have", "need", "want",
    "ticket", "name", "username", "ign", "igt", "account", "coords",
}


def decode_transcript(raw: str) -> List[dict]:
    """The message list out of a transcript's HTML, or [] if it is not one.

    Tolerant on purpose: a channel of 1,400 transcripts spanning years will contain files
    written by older versions of Ticket Tool, and one unreadable file must not stop the sweep.
    """
    m = re.search(r'(?s)let\s+messages\s*=\s*"([^"]*)"', raw)
    if not m:
        return []
    blob = m.group(1)
    try:
        data = base64.b64decode(blob + "=" * (-len(blob) % 4))
        doc = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return []
    return doc if isinstance(doc, list) else []


def owner_from_fields(fields: Sequence[Tuple[Optional[str], Optional[str]]]
                      ) -> Tuple[Optional[int], Optional[str]]:
    """(ticket owner id, ticket name) from Ticket Tool's log embed fields.

    Takes (name, value) pairs rather than a discord Embed so it stays testable.
    """
    owner, ticket = None, None
    for name, value in fields:
        if not name or not value:
            continue
        if name.strip() == "Ticket Owner":
            digits = re.sub(r"[^0-9]", "", value)
            if digits:
                owner = int(digits)
        elif name.strip() == "Ticket Name":
            ticket = value.strip()
    return owner, ticket


def candidate_names(entries: List[dict], owner_id: Optional[int],
                    redactor=None) -> List[str]:
    """IGN candidates from a decoded transcript, the owner's own words first.

    Other people's messages are searched only as a fallback. A helper typing "I'll sort out
    SomeoneElse" is a real way to bind the wrong account to a Discord id, and the ledger is
    what later refuses somebody a kit -- so the person stating their own name always wins.

    `redactor` is `redact.redact`; passed in rather than imported so this module stays free of
    project imports and the tests can prove redaction ran BEFORE matching. That order matters:
    a coordinate run can look like a name to a loose pattern.
    """
    own: List[str] = []
    other: List[str] = []
    for entry in entries:
        if entry.get("bot"):
            continue
        body = entry.get("content") or ""
        if not body:
            continue
        if redactor is not None:
            body = redactor(body)[0]
        is_owner = str(entry.get("user_id") or "") == str(owner_id or "")
        for pat in NAME_PATTERNS:
            for hit in pat.findall(body):
                hit = hit.strip(" .,:;!?'\"`")
                if len(hit) < 3 or hit.lower() in STOPWORDS:
                    continue
                (own if is_owner else other).append(hit)
    seen: set = set()
    ordered: List[str] = []
    for n in own + other:
        if n.lower() not in seen:
            seen.add(n.lower())
            ordered.append(n)
    return ordered


def participants(entries: List[dict]) -> Dict[str, str]:
    """{discord id: display name} for every human in the transcript."""
    out: Dict[str, str] = {}
    for entry in entries:
        if entry.get("bot"):
            continue
        uid = str(entry.get("user_id") or "")
        if uid:
            out.setdefault(uid, entry.get("username") or entry.get("nick") or uid)
    return out
