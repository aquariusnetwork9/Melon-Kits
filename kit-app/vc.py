"""Client for api.2b2t.vc -- the five calls behind a reviewer card.

Three things about this API drive the whole design and are easy to get wrong:

1. **The rate limit is global, not per-IP.** `/chats` and friends sit in a bucket allowing
   5 permits per second across every caller on the internet, so your throughput depends on
   strangers' traffic and a burst you did not cause will 429 you. 429s carry **no
   Retry-After**. Hence the module-level pacer: it is shared by every client instance in the
   process, because per-instance pacing would let two concurrent reviews double the rate.
2. **A bad date returns 302, not 400** -- a redirect to the API explorer page. With redirects
   followed that reads as a successful empty response, which is silent wrong data rather than
   an error. Redirects are refused and non-JSON content types rejected.
3. **204 means "nothing", not "error".** An unknown player, or a real player with no deaths,
   both come back 204 with an empty body. Treating that as a failure would decline people for
   having a clean record.

Never logs a response body. Bodies are chat, chat contains coordinates, and a log is not the
place for either -- diagnostics carry status codes, byte counts and row counts only.
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

_LOG = logging.getLogger("melonkit.vc")

# Process-wide, deliberately. See point 1 above.
_PACE_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]

_BACKOFF = (2.0, 5.0, 10.0, 20.0, 40.0)


class VcError(RuntimeError):
    """A request that cannot be retried into working."""


class VcUnavailable(VcError):
    """Transient: the API is rate-limiting or down. Worth telling the user to retry."""


def dash_uuid(raw: str) -> str:
    """Mojang returns undashed UUIDs; api.2b2t.vc binds a real UUID and rejects undashed."""
    s = (raw or "").strip().replace("-", "")
    if len(s) != 32:
        return raw
    return "%s-%s-%s-%s-%s" % (s[0:8], s[8:12], s[12:16], s[16:20], s[20:32])


class Client(object):
    def __init__(self, cfg: Dict[str, Any], log: Optional[logging.Logger] = None) -> None:
        vc = cfg["vc"]
        self._host = vc["host"]
        self._ua = vc["user_agent"]
        self._timeout = float(vc["request_timeout_s"])
        self._interval = float(vc["min_interval_s"])
        self._retries = int(vc["max_retries"])
        self._log = log or _LOG

    # ------------------------------------------------------------------ plumbing

    def _pace(self) -> None:
        with _PACE_LOCK:
            wait = self._interval - (time.monotonic() - _LAST_REQUEST[0])
            if wait > 0:
                time.sleep(wait)
            _LAST_REQUEST[0] = time.monotonic()

    def _get(self, path: str, params: Optional[List[Tuple[str, Any]]] = None) -> Optional[Any]:
        """GET and decode JSON. Returns None for 204. Raises VcError / VcUnavailable."""
        query = urllib.parse.urlencode([(k, str(v)) for k, v in (params or [])
                                        if v is not None])
        url = path + ("?" + query if query else "")
        attempt = 0

        while True:
            self._pace()
            try:
                conn = http.client.HTTPSConnection(self._host, timeout=self._timeout)
                try:
                    conn.request("GET", url, headers={
                        "User-Agent": self._ua, "Accept": "application/json"})
                    resp = conn.getresponse()
                    status = resp.status
                    ctype = resp.getheader("Content-Type") or ""
                    body = resp.read()
                finally:
                    conn.close()
            except (OSError, http.client.HTTPException) as exc:
                if attempt >= self._retries:
                    raise VcUnavailable("%s did not respond: %s"
                                        % (self._host, type(exc).__name__))
                self._log.warning("vc request failed path=%s attempt=%d err=%s",
                                  path, attempt, type(exc).__name__)
                time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                attempt += 1
                continue

            if status == 204:
                return None

            if status == 429:
                if attempt >= self._retries:
                    raise VcUnavailable(
                        "api.2b2t.vc is rate-limiting (429). The limit is shared globally "
                        "across all callers, so this is often not our traffic.")
                self._log.info("vc 429 path=%s attempt=%d", path, attempt)
                time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                attempt += 1
                continue

            if status in (301, 302, 303, 307, 308):
                # Permanent bug in our own request -- almost always an unparseable date.
                raise VcError("api.2b2t.vc redirected %s (HTTP %d): a bound was "
                              "unparseable, so this request can never succeed" % (path, status))

            if status == 400:
                raise VcError("api.2b2t.vc rejected %s (HTTP 400)" % path)

            if status >= 500:
                if attempt >= self._retries:
                    raise VcUnavailable("api.2b2t.vc returned HTTP %d" % status)
                time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                attempt += 1
                continue

            if status != 200:
                raise VcError("api.2b2t.vc returned HTTP %d for %s" % (status, path))

            if not ctype.lower().startswith("application/json"):
                raise VcError("api.2b2t.vc returned a non-JSON 200 for %s (content-type "
                              "%.40s) -- probably the API explorer page" % (path, ctype))

            try:
                return json.loads(body.decode("utf-8", "replace"))
            except ValueError as exc:
                # Never interpolates `body`: it is chat.
                raise VcError("api.2b2t.vc returned unparseable JSON for %s (%d bytes, %s)"
                              % (path, len(body), type(exc).__name__))

    @staticmethod
    def _ident(uuid: Optional[str], name: Optional[str]) -> List[Tuple[str, Any]]:
        """Prefer UUID. A name is a moving target -- a rename silently re-points it."""
        if uuid:
            return [("uuid", dash_uuid(uuid))]
        if name:
            return [("playerName", name)]
        raise ValueError("need a uuid or a playerName")

    # --------------------------------------------------------------------- calls

    def stats(self, uuid: Optional[str] = None,
              name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """`/stats/player` -- firstSeen, lastSeen, playtime and every count in one request.

        The single most efficient call on the API for this app's purposes: it replaces a
        `/seen` call plus four count queries, which matters against a shared rate limit.
        """
        return self._get("/stats/player", self._ident(uuid, name))

    def deaths(self, uuid: Optional[str] = None, name: Optional[str] = None,
               limit: int = 3) -> Dict[str, Any]:
        """`/deaths` -- the highest-value call for this app.

        A death from eleven minutes ago with its death message and killer attached *is* the
        "I lost everything" claim, already verified.
        """
        params = self._ident(uuid, name) + [("pageSize", max(1, min(int(limit), 100)))]
        doc = self._get("/deaths", params)
        return doc or {"deaths": [], "total": 0, "pageCount": 0}

    def kills(self, uuid: Optional[str] = None, name: Optional[str] = None,
              limit: int = 3) -> Dict[str, Any]:
        params = self._ident(uuid, name) + [("pageSize", max(1, min(int(limit), 100)))]
        doc = self._get("/kills", params)
        return doc or {"kills": [], "total": 0, "pageCount": 0}

    def chats(self, uuid: Optional[str] = None, name: Optional[str] = None,
              limit: int = 100) -> Dict[str, Any]:
        """`/chats` -- recent public chat, newest first.

        Note the field-name trap: rows here carry **`uuid`**, while the SSE feed's rows carry
        `playerUuid`. Reading the wrong one yields rows that look fine and have null UUIDs.
        """
        params = self._ident(uuid, name) + [
            ("pageSize", max(1, min(int(limit), 100))),
            ("sort", "DESC"),
        ]
        doc = self._get("/chats", params)
        return doc or {"chats": [], "total": 0, "pageCount": 0}

    def connections(self, uuid: Optional[str] = None, name: Optional[str] = None,
                    limit: int = 4) -> Dict[str, Any]:
        params = self._ident(uuid, name) + [("pageSize", max(1, min(int(limit), 100)))]
        doc = self._get("/connections", params)
        return doc or {"connections": [], "total": 0, "pageCount": 0}
