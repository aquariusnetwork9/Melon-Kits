"""Minecraft name -> UUID, and name history.

**A Minecraft account's creation date is not obtainable** and this module does not pretend
otherwise. Mojang only ever exposed it to the authenticated owner; the `?at=` binary-search
trick has been silently ignored since November 2020 (it does not error, it just returns the
current value, so code depending on it appears to work); `/user/profiles/{uuid}/names` has
404'd since September 2022. Every site still showing a "created" date is re-serving Ashcon's
frozen pre-2020 estimate, as Ashcon's own docs say. The substitute is 2b2t `firstSeen`, which
is more meaningful for vetting on this server anyway and cannot be bought.

Name history comes from laby.net, the only working source. **NameMC is a dead end** -- no
public API, a Cloudflare challenge on server-side fetch, and a robots.txt that names
ClaudeBot and GPTBot explicitly. It holds nothing laby.net doesn't.

Resolving the UUID matters even though `/stats/player` accepts a name: a rename silently
re-points a name at a different account, so a ledger keyed on names would lose a cooldown the
moment someone renamed, and a flag set on a name would follow the name to whoever took it.
"""
from __future__ import annotations

import http.client
import json
import logging
from typing import Any, Dict, List, Optional

from vc import dash_uuid

_LOG = logging.getLogger("melonkit.identity")

# Mojang's own bounds. Enforced locally so an obviously-invalid name never becomes a request.
_MIN_LEN, _MAX_LEN = 3, 16
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class IdentityError(RuntimeError):
    pass


class UnknownPlayer(IdentityError):
    """No Mojang account with that name."""


def valid_name(name: str) -> bool:
    name = (name or "").strip()
    return (_MIN_LEN <= len(name) <= _MAX_LEN
            and all(ch in _ALLOWED for ch in name))


def _get_json(host: str, path: str, ua: str, timeout: float = 10.0):
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": ua, "Accept": "application/json"})
        resp = conn.getresponse()
        status, body = resp.status, resp.read()
    finally:
        conn.close()
    if status in (204, 404):
        return status, None
    if status != 200:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return status, None


def resolve(name: str, cfg: Dict[str, Any],
            log: Optional[logging.Logger] = None) -> Dict[str, Optional[str]]:
    """``{'uuid': dashed, 'name': canonical_case}``. Raises UnknownPlayer / IdentityError.

    Returns Mojang's canonical capitalisation, not what the applicant typed -- so the ticket,
    the ledger and any flag all agree on one spelling.
    """
    log = log or _LOG
    name = (name or "").strip()
    if not valid_name(name):
        raise IdentityError(
            "%r is not a valid Minecraft name (3-16 characters, letters, digits and "
            "underscore only)" % (name,))

    host = cfg["identity"]["mojang_host"]
    ua = cfg["vc"]["user_agent"]
    try:
        status, doc = _get_json(host, "/users/profiles/minecraft/%s" % (name,), ua)
    except (OSError, http.client.HTTPException) as exc:
        raise IdentityError("could not reach %s (%s)" % (host, type(exc).__name__))

    if doc is None:
        if status in (204, 404):
            raise UnknownPlayer("no Minecraft account is named %r" % (name,))
        raise IdentityError("%s returned HTTP %d looking up %r" % (host, status, name))

    raw = doc.get("id") or ""
    canonical = doc.get("name") or name
    if not raw:
        raise IdentityError("%s returned no id for %r" % (host, name))
    return {"uuid": dash_uuid(raw), "name": canonical}


def name_history(uuid: str, cfg: Dict[str, Any],
                 log: Optional[logging.Logger] = None) -> List[Dict[str, Optional[str]]]:
    """Past names from laby.net, newest first. ``[]`` on any failure.

    Never raises. It is unofficial, informally rate-limited at roughly 10 requests a minute,
    and strictly a nice-to-have -- a review must not fail because a third-party name lookup
    was briefly unavailable.
    """
    log = log or _LOG
    if not cfg["identity"]["name_history"] or not uuid:
        return []
    host = cfg["identity"]["laby_host"]
    try:
        status, doc = _get_json(host, "/api/v3/user/%s/names" % (uuid,),
                                cfg["vc"]["user_agent"])
    except (OSError, http.client.HTTPException) as exc:
        log.info("name history unavailable host=%s err=%s", host, type(exc).__name__)
        return []
    if not isinstance(doc, list):
        if status != 200:
            log.info("name history host=%s http=%d", host, status)
        return []

    out: List[Dict[str, Optional[str]]] = []
    for entry in doc:
        if not isinstance(entry, dict):
            continue
        nm = entry.get("name") or entry.get("username")
        if not nm:
            continue
        out.append({"name": str(nm), "changed_at": entry.get("changed_at")
                    or entry.get("changedAt") or None})
    # laby.net has returned oldest-first historically; sort so the caller need not care.
    out.reverse()
    return out[:12]
