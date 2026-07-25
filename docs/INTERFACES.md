# Module interface contract — BINDING

`docs/SPEC.md` is the behavioural specification. **This file is the API contract.** Where they appear to
conflict on a signature, this file wins; where they conflict on behaviour, SPEC.md wins. Do not invent
signatures, do not rename anything here, do not add required parameters.

## Hard platform rules

- **Target Python 3.9.** The dev box is 3.9.13. Deployment may be newer, so 3.9 is the floor.
  - Every module starts with `from __future__ import annotations`.
  - **No `X | Y` unions** outside annotations. Use `typing.Optional` / `typing.Union` in runtime positions.
  - No `match` statements. No `tomllib`. No `except*`. No PEP-604 in `isinstance`/`cast`.
  - Builtin generics (`dict[str, int]`) are fine *in annotations only*, thanks to the `__future__` import.
- **Standard library only.** No pip installs, no vendored packages, ever. `zoneinfo` is permitted
  (present on 3.9) but must degrade per SPEC §9 when the tz database is absent.
- **Cross-platform.** Dev on Windows, deploy on Linux. Directory `fsync` and `fcntl` are POSIX-only —
  guard both. `os.replace` is used for every atomic swap.
- **UTC everywhere.** All internal time is `int` microseconds since the Unix epoch, named `*_us`.
  Never a float, never a naive `datetime`, never `time.mktime`, never `datetime.timestamp()`.

## Hard project rules

- **No chat content in any log, status file, alert, or exception message.** Ever. Log `row_id`, `ts`,
  `player_uuid`, counters, and byte offsets. This is a standing operator order, not a preference —
  2b2t chat routinely contains coordinates. A traceback that interpolates a record body is a defect.
- Every module is importable without side effects. No work at import time.
- No `print()` outside `collector.py`'s CLI paths and `tools/`.

---

## `tsutil.py` — build and unit-test FIRST

```python
class TsParseError(ValueError): ...

def parse_ts(s: str) -> int
    """Any source timestamp -> ts_us. Implements SPEC §3.1: regex parse, 0-9 fractional
    digits, right-pad to 9 then FLOOR-truncate to 6, missing zone means UTC.
    Raises TsParseError. Never uses datetime.fromisoformat."""

def fmt_ts(us: int) -> str
    """-> 'YYYY-MM-DDTHH:MM:SS.ffffffZ'. Always exactly 6 fractional digits, always 'Z'."""

def fmt_bound(us: int) -> str
    """-> 'YYYY-MM-DDTHH:MM:SS.ffffff'. NO zone suffix, exactly 6 digits. SPEC §3.3 —
    /chats/window binds LocalDateTime and silently discards any offset."""

def now_us() -> int
def utc_date(us: int) -> str          # 'YYYY-MM-DD'
def to_dump_csv_ts(us: int) -> str    # SPEC §3.4, for tools/export_csv.py only
```

## `record.py`

```python
KEYS: tuple           # the 12 canonical keys, SPEC §2 order. Authoritative.
SRC_SSE = "sse"; SRC_BACKFILL = "rest-backfill"; SRC_LOG = "proxy-log"

class RecordError(ValueError): ...

def row_id(ts_us: int, uuid: Optional[str], name: str, chat: str) -> str
    """SPEC §4.2. sha256 over b'\\x1f'-joined (str(ts_us), ident, chat)[:32].
    ident = uuid.lower() if uuid else 'name:' + name."""

def from_sse(obj: dict, *, seq: int, batch: str, ingest_us: int) -> dict
def from_window(obj: dict, *, seq: int, batch: str, ingest_us: int) -> dict
def from_logline(ts_us: int, name: str, chat: str, *, seq: int, batch: str, ingest_us: int) -> dict
    """Two SEPARATE normalizers for SSE and window — no shared fallback accessor chain
    (SPEC §2.1: SSE says 'playerUuid', window says 'uuid'). from_logline sets
    player_uuid=None, component=None, precision='s', and an 'L'-prefixed row_id."""

def encode(rec: dict) -> bytes
    """Exactly one line including the trailing b'\\n'. json.dumps with
    separators=(',',':'), ensure_ascii=False, sort_keys=False, keys in KEYS order."""
```

## `statefile.py`

```python
STATE_VERSION = 1

def new_state(now_us: int) -> dict
def load_state(path: str, log: logging.Logger) -> Optional[dict]
    """state.json, then state.json.bak (log ERROR if falling back). None if neither is
    usable -> caller treats as first run: hwm_us = now, NO backfill (SPEC §7.9)."""
def save_state(path: str, obj: dict) -> None      # atomic, SPEC §10
```

## `writer.py` — owns every byte on disk

**Deviation from SPEC §1, deliberate:** the spec routes producers through a `queue.Queue`. Use an
internal `threading.RLock` and direct method calls instead. Rationale: observed volume is a few rows
per *minute*, so decoupling buys nothing, and `enqueue_gap` must be durable *before* the SSE reader
reconnects (SPEC §6.5) — which a fire-and-forget queue cannot express without an ack round-trip.
All public methods are thread-safe. Document this deviation in the module docstring.

```python
class Writer:
    def __init__(self, cfg: dict, state: dict, log: logging.Logger) -> None
        """Performs startup repair (SPEC §5.4), stray .gz.tmp cleanup, pending_gzip
        re-drive, and dedupe rehydration anchored on state['hwm_us'] (SPEC §4.3b)."""

    def next_seq(self) -> int
    def put_record(self, rec: dict) -> bool
        """False == duplicate, dropped. Branches on rec['src']: SRC_LOG rows use a
        separate dedupe namespace, never advance hwm_us, never create gaps (SPEC §8.5)."""

    def enqueue_gap(self, start_us: int, end_us: int, *, pass_no: int = 1,
                    not_before_us: Optional[int] = None, tag: Optional[str] = None) -> Optional[str]
        """Clamps to backfill.max_lookback_days (SPEC §7.9). Returns None when the gap is
        skipped (zero-length -> would 400, SPEC §6.5). PERSISTS STATE BEFORE RETURNING."""

    def claim_gap(self) -> Optional[dict]                       # oldest runnable, marks running, persists
    def advance_gap(self, gap_id: str, cursor_us: int, rows_added: int) -> None   # persists every page
    def finish_gap(self, gap_id: str, status: str) -> None      # queues pass 2 per SPEC §7.7

    def hwm_us(self) -> int
    def bump(self, counter: str, n: int = 1) -> None
    def tick(self) -> None            # rotation, gzip drive, periodic fsync + state persist
    def snapshot(self) -> dict        # counters + storage facts for status.json. NO chat content.
    def close(self) -> None           # final fsync + state persist; idempotent
```

## `config.py`

```python
DEFAULTS: dict            # exactly SPEC §9, same nesting

class ConfigError(ValueError): ...

def load_config(path: Optional[str]) -> dict
    """Deep-merge DEFAULTS <- file <- env (CHATCOL_<UPPER_SNAKE>, nested joined by '_').
    Raises ConfigError on ANY unknown key or type mismatch (SPEC §9)."""
```

## `singlelock.py`

```python
class AlreadyRunning(RuntimeError):
    pid: Optional[int]

def acquire(path: str) -> object
    """flock on POSIX, msvcrt.locking on Windows. Writes our PID into the file.
    Returned handle must be kept alive for the process lifetime. Raises AlreadyRunning."""
```

## `status.py`

```python
class StatusPublisher:
    def __init__(self, cfg: dict, log: logging.Logger) -> None
    def update(self, section: str, **fields) -> None     # thread-safe merge
    def publish(self, writer: "Writer") -> None          # atomic write of status.json
    def snapshot(self) -> dict

def evaluate_health(st: dict, cfg: dict) -> tuple        # (code:int 0|1|2, reason:str)
def fire_alert(cfg: dict, log, state_name: str, reason: str) -> None   # on TRANSITION only
def run_health_check(cfg: dict) -> int                   # reads status.json, returns exit code
```

Health thresholds are SPEC §11.3 exactly. `reason` must never contain chat content.

## `ssereader.py`

```python
class SseReader(threading.Thread):
    def __init__(self, cfg: dict, writer: "Writer", status: "StatusPublisher",
                 log: logging.Logger, stop: threading.Event) -> None
    def run(self) -> None
```

`http.client.HTTPSConnection` only — never `urllib.request`, never redirect following. Exactly one open
connection per process, asserted. Frame parser per SPEC §6.2, watchdog §6.3, close classification and
backoff §6.4, gap emission §6.5 (persist the gap *before* reconnecting).

## `backfiller.py`

```python
class Backfiller(threading.Thread):
    def __init__(self, cfg: dict, writer: "Writer", status: "StatusPublisher",
                 log: logging.Logger, stop: threading.Event) -> None
    def run(self) -> None
```

Time-slice paging only — **never send `page`** (SPEC §7.3). Reject non-`application/json`, treat 302 and
400 as permanent bugs that alarm and never retry (SPEC §7.5). Token bucket at
`backfill.min_interval_s`. Owns the weekly audit job (SPEC §7.8).

## `collector.py`

```python
def main(argv: Optional[list] = None) -> int
```

CLI: `--config PATH`, `--health`, `--version`, `--print-config`. Acquires the lock (exit 3 if held),
loads state, builds `Writer`, starts `Backfiller` then `SseReader`, installs SIGINT/SIGTERM handlers
that set the stop event, runs the status/tick loop at `ops.status_interval_s`, joins with a timeout,
`writer.close()`. Never let an exception in one thread kill the process silently — a dead thread must
flip health to critical.

---

## Test conventions

`unittest`, stdlib only, runnable as `python -m unittest discover -s tests -v`. No network in any test —
fake the HTTP layer with a `http.server.ThreadingHTTPServer` on `127.0.0.1:0`. No test may contain real
2b2t coordinates or real chat text; use `"hello"`, `"gg"`, and obviously-synthetic strings.

The two non-negotiable tests from SPEC §13 are `tests/test_killnine.py` and `tests/test_boundary_row.py`.
