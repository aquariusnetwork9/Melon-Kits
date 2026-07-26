"""Configuration for the kit-request app.

One JSON file, every key also settable as ``MELONKIT_<UPPER_SNAKE>`` with nested paths
joined by ``_``. **An unknown key -- in the file or the environment -- is a hard startup
failure**, same rule as the collector: a typo'd key that silently keeps its default is how
a long-running deployment ends up quietly misconfigured.

The bot token is deliberately *not* a config key. It is read from the environment named by
``discord.token_env`` so it never lands in a file that might get committed.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Mapping, Optional, Tuple

ENV_PREFIX = "MELONKIT_"

DEFAULTS: Dict[str, Any] = {
    "discord": {
        # Name of the environment variable holding the bot token -- never the token itself.
        "token_env": "MELONKIT_DISCORD_TOKEN",
        # The bot is multi-guild: channels and roles live in the database per server, set by
        # /setup, not here. The five ids below are ONLY a bootstrap for the original
        # single-guild deployment -- on first start they are adopted into that guild's stored
        # config and then ignored forever. Leave them 0 on a fresh install.
        #
        # home_guild_id additionally gets a guild-scoped command copy so command edits appear
        # instantly there rather than waiting on Discord's global propagation. Harmless to set
        # on any install; it does not make the bot single-guild.
        "home_guild_id": 0,
        # Read-only TEXT channel holding the pinned request panel. Private ticket threads
        # hang off it. It cannot be a forum: forum posts can only be public threads, so
        # every applicant would be able to read every other applicant's ticket.
        "panel_channel_id": 0,
        # Staff-only FORUM channel. One post per ticket, carrying the reviewer card, the
        # decision and the delivery claim -- the whole lifecycle in one place, with tags for
        # state. A text channel also works; the forum just makes it filterable.
        "queue_channel_id": 0,
        # Staff-only archive. Every finished ticket -- delivered, declined or closed -- gets
        # one self-contained summary posted here, with the chat log attached, so the record
        # survives the thread and the queue post being deleted. 0 disables it.
        "transcript_channel_id": 0,
        # Include the applicant<->staff conversation in the transcript.
        #
        # This needs NO privileged intent. MESSAGE_CONTENT gates content in gateway events,
        # not in REST history fetches -- those only need Read Message History, which the bot
        # already has in its own ticket threads. Verified against real messages.
        #
        # Default off purely because it is a judgement call about recording people's
        # conversations, not because of a technical obstacle.
        "capture_thread_messages": True,
        # May approve/decline and see reviewer cards.
        "reviewer_role_id": 0,
        # May claim a dispatch. 0 means "anyone in the dispatch channel".
        "runner_role_id": 0,
    },
    # Panel copy that changes over time. Kept in config so updating it is an edit plus one
    # command -- no code change, no redeploy, and `--post-panel` edits the pinned message in
    # place rather than posting a second one.
    "panel": {
        "rescued_count": "15.1k",
        "rescued_as_of": "7/20/25",
        "response_time": "24-48 hours",
    },
    "policy": {
        "cooldown_days": 21,
        "max_open_tickets_per_user": 1,
        # How much recent chat to put in front of a reviewer.
        "recent_chats": 100,
        # How far back the screening sweep looks, and how many 100-row pages it will spend
        # getting there. `chat_window_days: 0` restores the old behaviour -- "the most recent
        # `recent_chats` lines, whenever they happened to be said".
        #
        # Both numbers come from measuring the 2025 bulk dump rather than from taste. The median
        # player said **12 lines in the entire year**; p90 is 244 and p95 is 574. So five pages
        # cover ~94% of players completely, for at most five requests instead of one. The cap
        # exists for the tail: p99 is 2,595 lines (26 requests) and the busiest account of 2025
        # said 109,220 (1,093 requests) -- which no single ticket may spend against a bucket
        # shared with every other caller of the API.
        "chat_window_days": 365,
        "chat_max_pages": 5,
        "recent_deaths": 3,
        # A death inside this window is treated as supporting "I just lost everything".
        "wipe_window_hours": 48,
        # firstSeen inside this window supports "just getting started".
        "new_player_days": 30,
    },
    "vc": {
        "host": "api.2b2t.vc",
        # Cloudflare 403s a default UA, and the maintainer deserves to know who is calling.
        "user_agent": "melon-kits/1.0 (+contact: set-me@example.com)",
        # The /chats bucket is 5 permits/second shared GLOBALLY across every caller on the
        # internet, not per-IP, and 429s carry no Retry-After. One request per second.
        "min_interval_s": 1.0,
        "request_timeout_s": 15,
        "max_retries": 4,
    },
    "identity": {
        # Resolve name -> UUID so a later rename cannot detach a ledger entry.
        "resolve_uuid": True,
        # laby.net is the only working name-history source; Mojang's died in Sept 2022 and
        # NameMC has no API. Unofficial, ~10 req/min -- treat a failure as non-fatal.
        "name_history": True,
        "laby_host": "laby.net",
        "mojang_host": "api.mojang.com",
    },
    "screening": {
        "enabled": True,
        # Populate from the 2025 bulk dump; see docs/chat-screening.md. None = no lexicon,
        # in which case chat is still listed for a human but nothing is counted.
        "lexicon_path": None,
        # Redact coordinates out of every chat line before it is shown. 2b2t chat leaks
        # third parties' base coordinates and a Discord thread is not the place for them.
        "redact_coords": True,
    },
    "store": {"path": "./melonkit.sqlite3"},
    "ops": {
        "log_path": None,
        "log_level": "INFO",
    },
}


class ConfigError(ValueError):
    """Raised for an unknown key, an untypeable value, or unreadable JSON."""


# Renamed keys, old -> new. An unknown key is a hard startup failure by design, so a rename
# would otherwise stop an existing deployment booting at all -- a rude way to ship an
# upgrade. Aliases are accepted and mapped.
_ALIASES = {("discord", "guild_id"): ("discord", "home_guild_id")}


def _apply_aliases(doc: Dict[str, Any]) -> None:
    for (sect, old), (nsect, new) in _ALIASES.items():
        block = doc.get(sect)
        if isinstance(block, dict) and old in block:
            doc.setdefault(nsect, {})
            if new not in doc[nsect]:
                doc[nsect][new] = block[old]
            del block[old]


def load_config(path: Optional[str] = None,
                env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        doc = _read_json(path)
        _apply_aliases(doc)
        _merge(cfg, doc, ())
    _apply_env(cfg, os.environ if env is None else env)
    _validate(cfg)
    return cfg


def env_name(dotted: str) -> str:
    """'vc.min_interval_s' -> 'MELONKIT_VC_MIN_INTERVAL_S'."""
    return ENV_PREFIX + dotted.replace(".", "_").upper()


# --------------------------------------------------------------------------- internals

def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc.strerror or exc))
    if not raw.strip():
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError("config %s is not valid UTF-8 JSON: %s" % (path, exc))
    if not isinstance(doc, dict):
        raise ConfigError("config %s must be a JSON object, got %s"
                          % (path, type(doc).__name__))
    return doc


def _merge(dst: Dict[str, Any], src: Mapping[str, Any], trail: Tuple[str, ...]) -> None:
    for key, val in src.items():
        here = trail + (str(key),)
        if key not in dst:
            raise ConfigError("unknown config key %r" % (".".join(here),))
        cur = dst[key]
        if isinstance(cur, dict):
            if not isinstance(val, Mapping):
                raise ConfigError("%s must be an object, got %s"
                                  % (".".join(here), type(val).__name__))
            _merge(cur, val, here)
        else:
            dst[key] = _coerce(val, cur, ".".join(here))


def _coerce(val: Any, default: Any, dotted: str) -> Any:
    """Accept a JSON value only if it fits the default's shape.

    None is always allowed: several keys are genuinely optional (lexicon_path, log_path)
    and their defaults are None, which carries no type information of its own.
    """
    if val is None or default is None:
        return val
    if isinstance(default, bool):
        if isinstance(val, bool):
            return val
        raise ConfigError("%s must be true or false" % dotted)
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ConfigError("%s must be a number" % dotted)
        return int(val)
    if isinstance(default, float):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ConfigError("%s must be a number" % dotted)
        return float(val)
    if isinstance(default, str):
        if not isinstance(val, str):
            raise ConfigError("%s must be a string" % dotted)
        return val
    return val


def _index(cfg: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Tuple[str, ...]] = {}

    def walk(node: Mapping[str, Any], trail: Tuple[str, ...]) -> None:
        for key, val in node.items():
            here = trail + (str(key),)
            if isinstance(val, dict):
                walk(val, here)
            else:
                out[env_name(".".join(here))] = here

    walk(cfg, ())
    return out


def _apply_env(cfg: Dict[str, Any], env: Mapping[str, str]) -> None:
    index = _index(DEFAULTS)
    for name, raw in sorted(env.items()):
        if not name.startswith(ENV_PREFIX):
            continue
        path = index.get(name)
        if path is None:
            # The token variable is intentionally outside the config surface, so it must
            # not be mistaken for a typo'd key.
            if name == cfg["discord"]["token_env"]:
                continue
            raise ConfigError(
                "unknown environment override %s (set %s=... for the bot token, or check "
                "the spelling against --print-config)" % (name, cfg["discord"]["token_env"]))
        node: Any = cfg
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = _coerce_env(raw, node[path[-1]], name)


def _coerce_env(raw: str, default: Any, name: str) -> Any:
    text = raw.strip()
    if isinstance(default, bool):
        low = text.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ConfigError("%s must be a boolean, got %r" % (name, raw))
    if default is None:
        return None if text == "" or text.lower() == "null" else text
    if isinstance(default, int):
        try:
            return int(text, 10)
        except ValueError:
            raise ConfigError("%s must be an integer, got %r" % (name, raw))
    if isinstance(default, float):
        try:
            return float(text)
        except ValueError:
            raise ConfigError("%s must be a number, got %r" % (name, raw))
    return text


def _validate(cfg: Dict[str, Any]) -> None:
    pol, vc = cfg["policy"], cfg["vc"]
    if pol["cooldown_days"] < 0:
        raise ConfigError("policy.cooldown_days must be >= 0")
    if not 1 <= pol["recent_chats"] <= 100:
        # /chats caps pageSize at 100 and page-walking a live player is not worth the
        # global rate-limit budget for a review that a human reads in one screen.
        raise ConfigError("policy.recent_chats must be between 1 and 100")
    if pol["recent_deaths"] < 1:
        raise ConfigError("policy.recent_deaths must be >= 1")
    if pol["chat_window_days"] < 0:
        raise ConfigError("policy.chat_window_days must be >= 0 (0 disables the window)")
    if not 1 <= pol["chat_max_pages"] <= 50:
        # 50 pages is 5,000 lines and ~50 requests for ONE ticket. Past that the honest answer
        # is that this API is the wrong tool -- /dump/player returns a whole history as one CSV,
        # at a server-wide concurrency of 1.
        raise ConfigError("policy.chat_max_pages must be between 1 and 50")
    if vc["min_interval_s"] < 0.2:
        raise ConfigError(
            "vc.min_interval_s below 0.2 -- the /chats bucket is 5 permits/second shared "
            "globally across every caller, so pacing faster mostly buys you 429s")
    if "contact" not in vc["user_agent"]:
        raise ConfigError(
            "vc.user_agent must carry a contact string; a default UA gets 403d by "
            "Cloudflare and an anonymous scraper is rude to a free API")
    if cfg["screening"]["enabled"] and not cfg["screening"]["redact_coords"]:
        raise ConfigError(
            "screening.redact_coords must stay true: chat excerpts routinely contain third "
            "parties' base coordinates and a Discord thread is not the place for them")

    # Channel collisions are a privacy failure, not a typo. panel_channel_id is PUBLIC;
    # queue and transcript are staff-only. Point two of them at one channel and reviewer
    # cards -- chat logs, reviewer flags, the ledger fan-out -- become world-readable, and
    # nothing at runtime would look wrong.
    ids = {"panel_channel_id": int(cfg["discord"]["panel_channel_id"] or 0),
           "queue_channel_id": int(cfg["discord"]["queue_channel_id"] or 0),
           "transcript_channel_id": int(cfg["discord"]["transcript_channel_id"] or 0)}
    seen = {}
    for key, val in ids.items():
        if not val:
            continue
        if val in seen:
            raise ConfigError(
                "discord.%s and discord.%s are the same channel (%d). The panel channel is "
                "public while the queue and transcript channels are staff-only, so sharing "
                "one would publish reviewer cards to applicants."
                % (seen[val], key, val))
        seen[val] = key
