# 2b2t Chat Corpus Collector — Build Specification v1

Consolidated from four research reports (`sse`, `backfill`, `proxylog`, `schema`). Everything below is implementable as-is. Section 12 lists every place the reports conflict or left something open, with the ruling the implementer must follow.

Confidence markers from the source reports are preserved inline as **[V]** = verified against source/live probe, **[L]** = likely, **[?]** = unconfirmed/unknown.

---

## 1. Architecture

Single Python process, stdlib only, one thread of control plus one worker thread for backfill.

```
                  ┌──────────────────────────────────────┐
  /feed/chats ───▶│ SSE reader (main loop, blocking)     │
   (SSE, 1 conn)  │  frame parse → normalize → dedupe    │──┐
                  └──────────────────────────────────────┘  │
                  ┌──────────────────────────────────────┐  ├─▶ Writer
  /chats/window ─▶│ backfill worker (1 thread, ≤1 req/s) │──┤   (append-only
   (REST, gaps)   │  gap queue from state.json           │  │    JSONL, daily
                  └──────────────────────────────────────┘  │    rotate+gzip)
                  ┌──────────────────────────────────────┐  │
  latest.log ────▶│ log-tail (optional, off by default)  │──┘
   (proxy, opt.)  └──────────────────────────────────────┘
                                                             ─▶ state.json (atomic)
                                                             ─▶ status.json (health)
```

Threads communicate through a single `queue.Queue` into the writer; the writer owns the file handles, the dedupe set, `state.json` and the high-water mark. Nothing else touches disk except the log-tail's own offset bookkeeping (which goes through the writer's state).

Hard invariants:
1. **At most one open `/feed/chats` connection per process, ever.** The edge enforces a 6-connection cap per IP shared across `/feed/*` **[V]**; leaked sockets manufacture their own 429s. Close the old socket before opening the new one, and assert `self._conn is None` before connecting.
2. **`state.json` never references data that has not been fsynced.** Append → fsync → then advance state. A crash therefore re-delivers, never loses.
3. **A closed (gzipped) day file is never reopened.** Backfilled rows for an old event time land in *today's* file (see §5).

---

## 2. Canonical record schema

One JSON object per line, UTF-8, bare `\n`, no BOM, `separators=(',',':')`, `ensure_ascii=False`, `sort_keys=False`. Emit **all 12 keys on every line, always, in exactly this order** — fixed key order makes the files diff and gzip well and lets a downstream reader use a fast path.

| # | key | type | notes |
|---|-----|------|-------|
| 1 | `ts` | string | Event time. `YYYY-MM-DDTHH:MM:SS.ffffffZ` — **fixed** 6 fractional digits, always `Z`. |
| 2 | `ts_us` | int | Microseconds since Unix epoch, UTC. Derived from `ts`; the two must always agree. |
| 3 | `player_uuid` | string \| null | Lowercase dashed 8-4-4-4-12. `null` when absent. Never substitute the name here. |
| 4 | `player_name` | string | Observation valid only at `ts`. Never the corpus identity key. |
| 5 | `chat` | string | Bare message body. No `<name>` prefix, no colour codes **[V]**. |
| 6 | `component` | string \| null | The SSE `component` value **stored verbatim as received** — a JSON string containing JSON **[V]**. Do not decode-and-re-encode. `null` for every non-SSE source. |
| 7 | `src` | string enum | `"sse"` \| `"rest-backfill"` \| `"proxy-log"`. |
| 8 | `precision` | string enum | Source-guaranteed resolution: `"us"` for `sse` and `rest-backfill`, `"s"` for `proxy-log`. See §12.2. |
| 9 | `row_id` | string | 32 lowercase hex chars (see §4). Proxy-log rows are prefixed `L` → 33 chars. |
| 10 | `ingest_ts` | string | Collector wall clock, same format as `ts`. |
| 11 | `seq` | int | Monotonic per-collector counter, persisted in `state.json`, never reset. Local receive order — the only defence against out-of-order SSE delivery **[V]**. |
| 12 | `batch` | string \| null | Provenance. `sse`: `"c<connection_ordinal>"`. `rest-backfill`: the `gap_id`. `proxy-log`: `"<basename>@<byte_offset>"`. |

Example (single line, wrapped here for readability):

```json
{"ts":"2026-07-25T20:37:13.404045Z","ts_us":1785012 ... ,"player_uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","player_name":"Notch","chat":"hello","component":"{\"text\":\"\",\"extra\":[...]}","src":"sse","precision":"us","row_id":"9f2c...","ingest_ts":"2026-07-25T20:37:13.612001Z","seq":184223,"batch":"c417"}
```

### 2.1 Field-name mapping across sources — the silent trap

Both reports flag this independently and it is the single easiest way to produce a corpus of `null` UUIDs **[V]**:

| canonical | SSE `/feed/chats` | REST `/chats/window` | bulk `chats.csv` | proxy `latest.log` |
|---|---|---|---|---|
| `ts` | `time` | `time` | `time` | prefix group 1 |
| `chat` | `chat` | `chat` | `chat` | tail after prefix |
| `player_name` | `playerName` | `playerName` | `player_name` | parsed from `<name>` |
| `player_uuid` | **`playerUuid`** | **`uuid`** | `player_uuid` | **not present** |
| `component` | `component` | *absent* | *absent* | *absent* (ANSI stripped) |

Write two explicit normalizers (`from_sse(obj)`, `from_window(obj)`). Do not write one accessor with a fallback chain — a fallback chain hides the day the API renames a field.

---

## 3. Timestamp handling

### 3.1 Parsing (input)

Do **not** use `datetime.fromisoformat`. The SSE feed emits **nanosecond** precision (9 fractional digits observed on every sampled event, 30-char strings) **[V]**, and the fractional width is **variable** because Jackson trims trailing zeros — 0 through 9 digits are all possible **[V]/[L]**. Widths of 0–6 were observed across the bulk dump and REST responses **[V]**. Never slice at a fixed offset.

Single parser, used for every source:

```python
_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,9}))?(Z|z|[+-]\d{2}:?\d{2})?$")
```

Rules:
- Missing zone → **assume UTC**. (`chats.csv` uses `+00`, REST/SSE use `Z` **[V]**.)
- Fraction: right-pad to 9, then **truncate (floor) to 6 digits**. Never round — rounding up can push the value into the next microsecond and corrupt the inclusive backfill cursor (§6.3).
- Compute `ts_us` with `calendar.timegm` on the date/time parts, then add the microseconds and subtract the offset. Never use `time.mktime` or naive `datetime.timestamp()` (local-timezone contamination).
- Parse failure → drop the record, increment `parse_errors`, log the exception **without the record body** (see §11.4).

Truncating nanoseconds to microseconds is deliberate: `public.chats` is `timestamptz(6)` **[V]**, so microseconds is the DB's own resolution and the only resolution at which an SSE row and its `/chats/window` twin can ever be made to collide.

### 3.2 Rendering (output)

`ts` is always fixed-width 6 digits + `Z`. Fixed width is load-bearing: byte-lexicographic order equals chronological order, which the dump's variable-width `+00` form does not guarantee **[V]**.

```python
def fmt_ts(us: int) -> str:
    s, frac = divmod(us, 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)) + ".%06dZ" % frac
```

### 3.3 Rendering for `/chats/window` query bounds

Different format, and getting it wrong fails **silently**. Bounds bind to `LocalDateTime`, so any zone suffix is parsed and then **discarded** **[V]** — sending a local offset queries the wrong instant with no error.

```python
def fmt_bound(us: int) -> str:          # NO 'Z', NO offset, UTC wall clock, exactly 6 digits
    s, frac = divmod(us, 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)) + ".%06d" % frac
```

Never emit only 3 fractional digits for a cursor bound: a real row at `.872508` provably vanishes when `startDate` is `.873` **[V]**.

### 3.4 Optional: dump-compatible CSV export

If the operator ever wants a file that concatenates with `chats.csv`, the byte-lossless conversion from canonical `ts` is: replace `T` with a space; replace `Z` with `+00`; strip trailing zeros from the fraction; if the fraction becomes empty, drop the decimal point too **[V]**. Header exactly `time,chat,player_name,player_uuid`, LF, no BOM, RFC4180 quoting (quote only on `,` `"` CR LF; escape `"` by doubling) **[V]**. Ship this as a separate `export_csv.py` tool, not in the collector's hot path.

---

## 4. Dedupe key and dedupe state

### 4.1 Why client-side dedupe is mandatory

- There is no `id:` on the SSE stream, no `Last-Event-ID` handling, and no server-side dedup **[V]**.
- `public.chats` has no primary key, unique key or index — byte-identical duplicate rows are structurally possible **[V]/[L]**.
- Each feed tears down and re-registers its Redis reliable-topic listener **once per hour**, a plausible replay window **[L]**.
- Both `/chats/window` bounds are **inclusive** **[V]**, so every stitched window re-delivers its boundary row by design.

### 4.2 The key

```python
def row_id(ts_us: int, uuid: str | None, name: str, chat: str) -> str:
    ident = uuid.lower() if uuid else "name:" + name
    blob  = b"\x1f".join((str(ts_us).encode(), ident.encode("utf-8"), chat.encode("utf-8")))
    return hashlib.sha256(blob).hexdigest()[:32]
```

- Key on the **integer** `ts_us`, never the raw string — the identical instant arrives as `.11` from one query and `.110000` from another **[V]**.
- `\x1f` cannot occur in any field, so the join is unambiguous.
- `player_uuid` is nullable and *is* null in older data (12/100 in a 2013 sample) **[V]**, so the `name:` fallback is required. `player_name` is NOT NULL **[V]**.
- 128 bits truncated: at 6.7M rows/year over 10 years, collision probability is ~1e-25. Fine.
- Collision *semantics*: a false merge needs the same player emitting byte-identical text in the same microsecond. Impossible for a single client the scraper records as one insert **[V]**. In microsecond-era data, `(time, uuid)` alone was already unique across 500 sampled rows **[V]**.

**Proxy-log rows use a separate namespace:** `"L" + sha256(...)` computed from the second-floored `ts_us`, the name (uuid is always absent), and the chat text. They can never match an SSE key and must never be entered into the SSE/backfill dedupe set. See §8.

### 4.3 Where dedupe state lives — bounded, and rebuilt from the corpus

Do not keep an unbounded set. Three tiers:

**(a) Live window set (in memory, bounded by event time).**
`dict[int, set[str]]` keyed by `ts_us // 60_000_000` (one-minute buckets) → row_ids. After each write, prune buckets whose minute is older than `max_ts_us_seen - dedupe_window_s`. Default `dedupe_window_s = 21600` (6 h) — comfortably wider than the 1-hour listener-refresh replay window **[L]**.

Sizing: 6.7M rows/yr ≈ 765 rows/hour average **[V, derived]**; observed quiet-window rate was ~3/min **[V]**, peak unmeasured **[?]**. 6 h at a pessimistic 10× average is ~46k keys ≈ 5 MB with Python overhead. Bounded and small. Add a hard cap `dedupe_max_keys = 400_000`; if exceeded, drop the oldest bucket and increment `dedupe_evictions` (a signal that the window is too wide for actual traffic).

**(b) Startup rehydration — the data file is the dedupe log.**
On start, read the active JSONL backwards (and prior `.jsonl.gz` files if needed) collecting `row_id` for every row with `ts_us >= hwm_us - dedupe_window_s*1e6`. Anchor on `hwm_us`, **not on `now`** — that is what makes the boundary row deduplicate correctly after arbitrarily long downtime (§6.4). Cap the rehydration read at `rehydrate_max_bytes = 256 MiB` and log if the cap truncates it.

**(c) Per-gap local set (backfill only).**
Each backfill pass keeps its own `set[str]`, discarded when the gap completes. Gaps are clamped to 7 days (§6.6) ≈ 128k rows worst case, so this is bounded at a few MB. This is what prevents an old-gap walk from being defeated by the event-time pruning in (a).

**(d) Belt and braces.** Ship `dedupe_report.py` that streams the whole corpus and reports duplicate `row_id`s. The collector never needs it, but months of unattended running deserve an auditable check. Run it monthly.

---

## 5. Files, rotation, gzip

### 5.1 Naming and partitioning

- Active file: `<data_dir>/chat-YYYY-MM-DD.jsonl` where the date is the **UTC date of `ingest_ts`**, not of `ts`.
- Closed file: `<data_dir>/chat-YYYY-MM-DD.jsonl.gz`.

**Partitioning by ingest date, not event date, is a deliberate decision.** Backfilled rows for a gap two days ago must not require reopening a closed, gzipped, fsynced file. Append-only stays append-only; there is exactly one writable file at any instant. Downstream mining sorts by `ts_us` anyway, and `src`/`ts` on every row make the reordering explicit. Do not "fix" this by routing backfill into per-event-date files.

### 5.2 Rotation

Checked on every write and on the 15 s status tick: if `utc_date(now) != active_date`, roll. Optional failsafe `max_file_bytes` (default 0 = disabled) rolls to `chat-YYYY-MM-DD.NN.jsonl` — the compressor and the reader must both glob `chat-*.jsonl*`.

Roll sequence, crash-safe at every step:
1. `flush()` + `os.fsync()` the active file, `close()`.
2. Append the closed name to `state["pending_gzip"]`, persist state atomically.
3. Open the new day's file (mode `"ab"`), persist state again with the new `active_file`.
4. Compress asynchronously in the writer's idle time.

### 5.3 Gzip

Grace period `gzip_delay_s` (default 3600) after the day closes, so a very-late backfill row still has a plaintext file to go to — no: it does **not**, because of §5.1. The grace exists purely to keep the most recent closed day cheaply greppable for the operator. It is optional; 0 is fine.

Compression, resumable:
1. `gzip.open(tmp, "wb", compresslevel=6)`, stream the source in 1 MiB chunks. `tmp = <name>.gz.tmp`.
2. `flush()`, `fsync()`, `close()`.
3. **Verify**: stream-decompress `tmp`, count `\n` and total bytes, compare against the source's counts. Mismatch → delete `tmp`, alarm, leave the plaintext in place, do not retry automatically.
4. `os.replace(tmp, <name>.gz)`; `fsync` the directory inside `try/except` (a no-op that raises on Windows).
5. `os.unlink(<name>.jsonl)`.
6. Remove from `state["pending_gzip"]`, persist.

On startup: delete every `*.gz.tmp`, then re-drive `state["pending_gzip"]`. `compresslevel=6` — level 9 costs ~2× CPU for ~1–2% on this data.

### 5.4 Startup repair of the active file

A hard kill can leave a torn final line. Before the first append:

1. If the file does not end in `\n`: read the last 1 MiB, find the last `\n`, `os.truncate` to just past it.
2. Read the now-final line; if `json.loads` fails, truncate that line too. Repeat up to 8 times.
3. `fsync`. Log `repaired_bytes=N` at WARNING.

This is the only place the collector ever truncates a data file, and it only ever removes bytes after the last valid record.

### 5.5 Durability of appends

- Open `"ab"` (append mode: the kernel positions at EOF per write, so a crashed process can never overwrite good data).
- Write each record as **one** `f.write(bytes)` ending in `\n`. Do not write the payload and the newline separately.
- `flush()` on every write (`buffering=0` is not usable with `"ab"` text; use binary and an explicit `bytearray` batch).
- `os.fsync()` at most every `fsync_interval_ms` (default 2000) **and unconditionally before any `state.json` advance**. This bounds worst-case re-delivery after a power loss to 2 s of chat (~1 row at observed rates), all of which is re-fetched by the reconnect gap anyway.

---

## 6. SSE consumption loop

### 6.1 Connection

`http.client.HTTPSConnection(host, timeout=socket_read_timeout_s)`; `GET /feed/chats`.

Headers:
```
Accept: text/event-stream
Accept-Encoding: identity          # never gzip — a decompression buffer hides heartbeats
Cache-Control: no-cache
User-Agent: <config: identifying string + contact>   # Cloudflare 403s urllib's default UA [V]
Connection: close
```

`socket_read_timeout_s` default **20**. Every `socket.timeout` on `read()` is a normal event: it is the watchdog tick. Do not treat it as an error.

Do **not** use `urllib.request` here, and do **not** enable redirect following anywhere in this program (§7.5).

### 6.2 Frame parsing

The wire format is exactly `data:{json}\n\n` — no space after the colon, LF only, no `event:`, no `id:`, no `retry:` **[V]**. Parse defensively anyway:

```
buffer = bytearray()
for each chunk read:
    last_inbound_monotonic = time.monotonic()
    buffer += chunk
    while b"\n" in buffer:
        line, buffer = split at first b"\n"
        line = line.rstrip(b"\r")                 # tolerate CRLF even though none is sent
        if line == b"":                           # dispatch
            if data_lines: emit("\n".join(data_lines)); data_lines.clear()
        elif line.startswith(b":"):               # comment -> heartbeat, liveness only
            heartbeats += 1
        elif line.startswith(b"data:"):
            v = line[5:]
            if v.startswith(b" "): v = v[1:]      # SSE spec allows one optional space
            data_lines.append(v)
        elif line.startswith((b"event:", b"id:", b"retry:")):
            log ONCE at INFO ("server started sending <field>") and ignore
        else:
            unknown_lines += 1
```

Do not JSON-parse comment lines. Do not register a named event handler — there is no `event:` line, so a listener bound to `"chat"` receives nothing, forever, silently **[V]**.

Decode the JSON payload with `errors="replace"` on the UTF-8 step and count replacements; a corpus is worth more than a crash.

### 6.3 Watchdog

Judge liveness from **inbound frames, not data**. A 50 s gap between chats is routine; the observed quiet-window rate was ~3/min with gaps up to 50.2 s **[V]**.

- Heartbeat comment `:heartbeat` is emitted by a **global** scheduler at a fixed 60.000 s rate, so its phase relative to your connect is arbitrary — the first one can legitimately arrive up to ~60 s after connect (16.0 s in one probe) **[V]**.
- Deadline: `now_monotonic - last_inbound_monotonic > heartbeat_deadline_s` (default **90**).
- Initial grace after connect: `initial_grace_s` (default **120**) before the deadline applies.
- On breach: close the socket, classify as `STALL`, reconnect.

### 6.4 Close classification and backoff

Three modes, three policies:

| mode | how detected | policy |
|---|---|---|
| `CLEAN_EOF` | `read()` returns `b""` | **Normal path.** Log at DEBUG, not ERROR. Sleep `uniform(0.5, 2.0)` s. |
| `BROKEN` | `socket.timeout` past deadline (`STALL`), `ConnectionResetError`, `IncompleteRead`, `ssl.SSLError`, any other exception | Exponential: `min(60, 1 * 2**n)` s + `uniform(0, 0.5*delay)` jitter. |
| `RATE_LIMITED` | HTTP 429 at connect | `min(300, 60 * 2**n)` s + jitter. No `Retry-After` is sent **[V]**; the cause is almost certainly your own connection count. |

Reset `n` to 0 after `backoff_reset_s` (default 60) of healthy streaming.

Expect `CLEAN_EOF` roughly **every 15 minutes** — `new SseEmitter()` inherits `spring.mvc.async.request-timeout=15m`, and the timeout is mapped to a 200/null-body which, on an already-committed chunked response, the client simply sees as EOF **[V]**. Additional unscheduled clean EOFs occur on every API deploy (`ContextClosedEvent` → `emitter.complete()`) **[V]**. **~96+ reconnects/day is healthy.** Do not alert on reconnect count alone; alert on reconnect *rate* (§11.3).

Non-429, non-200 status at connect (5xx, 502 from the edge): treat as `BROKEN`.

A 429 from the edge has `Content-Type: text/html`, `Server: cloudflare`, no `Retry-After`, ~0.12 s **[V]**. Log the content-type for diagnosis but **do not branch behaviour on the body** (§12.5).

### 6.5 Gap detection

Every disconnect is a hole: there is no `id:`/`Last-Event-ID`, so replay is impossible and REST backfill is mandatory **[V]**.

On every transition from *connected* to *disconnected*, and on every process start:

```
start_us = hwm_us - safety_lag_us            # default safety_lag_s = 120
end_us   = <wall clock at the moment the next connection is established> + 5_000_000
```

`hwm_us = max(ts_us)` over all rows written from `sse` or `rest-backfill`. Subtracting `safety_lag_us` is required because **events can arrive out of order** — each Redis message is fanned out on its own virtual thread with no sequencing, so `time` is not monotonically non-decreasing across the stream **[V]** (structurally possible; no inversion was actually captured in ~4 min of low-traffic probing **[?]**). Without the margin, an event that arrived late but was timestamped before `hwm_us` is never backfilled.

Overshoot in both directions is free: the overlap rows are inside the dedupe window by construction (the rehydration anchor is `hwm_us`, §4.3b, so the boundary row's key is always present no matter how long the downtime).

Enqueue the gap into `state["gaps"]` and persist **before** attempting the reconnect. Then, and only then, reconnect. A gap is never held only in memory.

**Do not use zero-length gaps.** If `end_us <= start_us`, skip: `startDate == endDate` returns **400**, not an empty result **[V]**.

### 6.6 What the loop does not do

- It does not sort. Buffer-and-sort was considered; the writer stays strictly append-in-receive-order and records `seq`. Any consumer that needs chronological order sorts by `ts_us` offline. This keeps the crash-safety story trivial.
- It does not poll `/chats/window` for the live edge, ever (§7.6).

---

## 7. Backfill via `/chats/window`

### 7.1 Gap queue (durable)

`state["gaps"]` is a list of:

```json
{"gap_id":"g000417","start_us":...,"end_us":...,"pass":1,"attempts":0,
 "not_before_us":...,"cursor_us":...,"rows":0,"status":"pending"}
```

`status ∈ {pending, running, done, error}`. `cursor_us` is persisted after **every successfully written page**, so a 429 storm, a restart, or a host reboot resumes mid-gap without re-walking **[V, both reports insist]**.

### 7.2 The request

```
GET /chats/window?startDate=<fmt_bound(cursor_us)>
                 &endDate=<fmt_bound(end_us)>
                 &sort=ASC
                 &pageSize=100
```

- `sort=ASC` **requires** `startDate` (400 without it); supplying both bounds is legal and applies both **[V]**.
- Always send an explicit `pageSize=100`. Default is 25, which quadruples request count against a scarce global limiter. `pageSize=0` → 204 (indistinguishable from no data); `pageSize=-1` → 302; `pageSize>100` → 400 **[V]**.
- Never send `page`. See §7.3.
- `endDate` is included so the cache key advances predictably and the window is bounded.

### 7.3 Paging = time-slicing, never offset

After each page: set `cursor_us = ts_us` of the **last row in the page** and re-issue with `page` omitted. Never increment `page`.

Rationale: `ORDER BY time` has **no tiebreaker column and there is no primary key** **[V]**, so offset paging is formally unsafe at a page boundary inside a tie group; and deep offsets beyond ~6900 are untested **[?]**. Time-slicing is immune to both, and to offset drift from concurrent inserts. Consistency and non-overlap of consecutive time slices were verified live **[V]**.

Because both bounds are inclusive **[V]**, the last row of page *N* is re-delivered as the first row of page *N+1*. **This is intended.** Never add an epsilon to the cursor to avoid it: `+1 ms` provably drops rows (`.872508` vanishes at `.873`) **[V]**, and `+1 µs` is only safe if you are certain no second row shares that exact microsecond. The dedupe key discards the boundary row for free.

### 7.4 Termination and the tie-group guard

Stop the gap when **either**:
- HTTP **204** (empty page — there is never a 200 with an empty array **[V]**), **or**
- `len(chats) < 100` (short page).

There is no `total`, no `pageCount`, no `hasMore`, no next-cursor — the response body's only top-level key is `chats` **[V]**.

**Non-advancing-cursor guard:** if a page's last `ts_us` equals the `cursor_us` you sent *and* every row in the page deduplicated away, you have hit a tie group larger than 100 rows. `pageSize` is already at max, so advance `cursor_us += 1`, increment `tie_skips`, and log at WARNING with the timestamp and the potential row loss. (Only reachable in whole-second legacy data — a 2013 sample had 6 tie groups over 100 rows; the 2026 sample had zero **[V]**. A gap-backfilling collector should never see it, so treat any occurrence as a signal that the lookback clamp misfired.)

### 7.5 Response validation — the 302 trap

**Disable redirect following** and **reject any response whose `Content-Type` is not `application/json`.** An unparseable date does not return 400: it returns **302 → `https://api.2b2t.vc/` → `/scalar` → 200 `text/html`** (the API Explorer page) **[V]**. With redirects on, a date-formatting bug looks like a successful empty poll and stalls the backfill forever, silently.

Four response classes, three of them with completely empty bodies **[V]**:

| status | meaning | action |
|---|---|---|
| 200 + `application/json` | data | ingest |
| 204 | end of window | complete the gap |
| **400** | missing sort anchor, `endDate <= startDate`, `pageSize > 100` | **permanent bug in your request.** `status = "error"`, alarm, **never retry.** |
| **302** | unparseable date | **permanent bug.** Same treatment as 400. |
| 429 | transient | retry with backoff |
| anything else / non-JSON content-type | treat as `error`, alarm |

### 7.6 Rate limiting and pacing

`/chats/window` sits in the resilience4j `main` bucket: **5 permits/second, GLOBAL across every caller of every `main` endpoint**, 10 s acquire timeout, then 429 with an empty body and no `Retry-After` **[V]**. Your throughput depends on strangers' traffic. Observed: **~18% of requests 429'd even while pacing at one request every 3–4 seconds**, and reported failures represent saturated periods lasting 75+ seconds **[V]**.

Therefore:
- `backfill_min_interval_s` default **1.0**, and the implementer should be comfortable raising it. Enforce with a monotonic-clock token bucket in the backfill thread.
- 429 backoff: `5, 10, 20, 40, 80, 160, 300…` capped at 300 s + `uniform(0, 0.3*d)`. Retry indefinitely — the gap is durable, there is no deadline.
- The SSE feed is in **no** rate-limit bucket and an open stream costs **zero permits** **[V]**. Never budget permits for it, and never let backfill backoff delay a reconnect. Separate threads, separate clocks.
- **Never re-issue an identical URL expecting fresher rows.** Responses are `@Cacheable("chatsWindow")` with a 5-minute Caffeine TTL (`maximumSize=250`); an identical URL returned byte-identical 12-row results 100 s apart while chat was flowing **[V]**. Time-slicing changes `startDate` every page, so the normal path is safe by construction. The one place to be careful is a second pass over the same gap (§7.7) — it runs ≥90 min later, far past the TTL.

### 7.7 When to backfill: immediate + confirming second pass

This is where the reports disagree (§12.1). The ruling:

**Pass 1 — immediately.** `not_before_us = now`. `/chats/window` queries Postgres directly via jOOQ and is *not* served from the 3-minute-lagged DuckDB replica that `/chats`, `/chats/search` and `/chats/word-count` use; the newest available row was 32 s behind wall clock **[V]**. Recent gaps are therefore fillable now, and a 15-minute reconnect hole should be closed in seconds, not in an hour.

**Pass 2 — the same range, re-queued at `end_us + 90 min`.** The source contains a deliberate `scraperTimeCutoff = now - 1 hour` constant, used solely to fence off window queries for newer-than-1h data, which signals that the maintainer considers the DB settled only about an hour back **[V]**; whether that implies incompleteness inside the last hour is **[?]**. Ingest lag was measured at exactly one point (~32 s), which conflates lag with the natural gap between messages **[?]**.

Pass 2 costs nothing but a re-walk, and dedupe makes it idempotent. Set `pass=2`, `not_before_us = end_us + 90*60*1e6`, `cursor_us = start_us`. When pass 2 completes, log `rows_added_pass2`; a persistently non-zero value is empirical proof the 1-hour settle window is real and is worth surfacing in `status.json`.

### 7.8 Weekly completeness audit (recommended, default on)

The SSE feed is **not** derived from database rows: each `LiveFeed` subscribes to a Redisson `RReliableTopic` ("ChatsTopic") published by rfresh's external bot fleet, while the Postgres `CHATS` table is written by a wholly separate ingest path. **There is no code-level guarantee that the SSE set equals the DB set, in either direction** **[V]**, and which way it differs is **[?]**.

So: once a week, enqueue a synthetic gap covering `[now - 8 days, now - 1 day]` with `pass="audit"`. At 128k rows/week that is ~1,290 requests ≈ 22 min at 1 req/s. Report `audit_rows_added` in `status.json`. If it is consistently zero, the feed is a superset in practice and the operator can lengthen the interval. If it is not zero, the audit is the only thing standing between the operator and a quietly lossy corpus.

### 7.9 Lookback clamp

There is **no server-side maximum window span** — a 19-year window (2011→2030) is accepted without complaint and returns the oldest data in the DB (earliest chat is 2013-11-18) **[V]**. A corrupted or absent `hwm_us` would therefore trigger a full-history crawl.

Clamp: `start_us = max(start_us, now - backfill_max_lookback_days*86400e6)`, default **7 days**. If the clamp fires, log at ERROR, fire `alert_command`, and record `clamped: true` on the gap. If `state.json` is missing entirely (first run), do **not** backfill at all — set `hwm_us = now` and start clean.

---

## 8. Optional source: tailing an AquariusProxy bot's log

**Default `log_tail.enabled = false`.** This source is materially lower fidelity than SSE and mixing it in by default would degrade the corpus. Turn it on when the operator wants redundancy on a box where a plugin cannot run.

### 8.1 Why it exists

Plugin jars in `plugins/` are **not loaded at all under GraalVM native-image runtime** — `PluginManager.preLoadPlugins()` checks `ImageInfo.inImageRuntimeCode()` and returns after firing a Discord warning **[V]**. A plugin-based collector silently collects nothing on the linux/native channel. File logging, by contrast, works identically on the java and native channels (`LazyInitRollingFileAppender` no-ops only at image **build** time) **[V]**. So log-tailing is the only zero-deploy, all-channel path. (Note: a collector compiled onto the system classpath *would* load, because `preLoadPluginsFromSystemClasspath()` runs at line 123, **before** the native guard at 124 **[V]** — out of scope here.)

### 8.2 What the log gives, and what it costs

Chat logging is **on by default** (`CONFIG.client.extra.logChatMessages = true`) **[V]** — but it is runtime-togglable via the `extrachat logChatMessages off` / `terminal logChatMessages off` commands **[V]**, so a live operator can silence the feed with no error. Alarm on unexpected `[Chat]` silence; never read silence as quiet chat.

Line format **[V, derived from the logback pattern and the ANSI-strip encoder — no real `[Chat]` line was ever sampled, see §12.6]**:

```
[yyyy/MM/dd HH:mm:ss] [Chat] [INFO] <chat text>
```

Local time, **second resolution, no timezone offset**. All ANSI and section codes are stripped by `%stripAnsi`, so colour, rank formatting, and click/hover data are unrecoverable **[V]**. And critically: **there is no UUID anywhere in the line** **[V]**. Log-derived rows are name-only.

### 8.3 Parsing

```python
LINE = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\] \[Chat\] \[INFO\] (.*)$")
```

Any line that does **not** match the prefix is a **continuation** of the previous event (embedded newlines in a component produce unprefixed physical lines **[L]**) — append it to the pending event's text rather than dropping it, or multi-line system messages truncate silently.

`[Chat]` is **not chat-only**. `SystemChatHandler` logs at line 99 *before* the schema parse at line 54, so death messages, server broadcasts, join/leave spam, restart notices and command output all land on `[Chat] [INFO]` indistinguishably; only overlay/action-bar text is excluded **[V]**. Two proxy-internal sources also share the logger: DM echoes (`Proxy.java:869`) and synthesized `Position in queue: N` lines (`ExtraChat.java:93`) **[V]**.

**Whitelist by shape; never blacklist noise.** Reimplement the default `ChatSchema` in the order the proxy's parser uses — whispers **before** public **[V]**:

1. `^(?P<s>[\w\d_.]+) whispers: (?P<m>.+)$` → **drop** (inbound whisper)
2. `^to (?P<r>[\w\d_.]+): (?P<m>.+)$` → **drop** (outbound whisper)
3. `^<(?P<s>[\w\d_.]+)> (?P<m>.+)$` → **keep** as public chat
4. anything else → **drop**

Sender/receiver charset `[\w\d_.]+`, message greedy `.+` **[V]**. The default schema applies to every server including 2b2t, because `serverSchemas` is an empty map by default **[V]** — but if the operator has configured an override for the connected address, the tailer must read the bot's `config.json` and use that schema instead. Config key: `log_tail.schema_from_config_path`.

Accept that this is **best-effort**: the proxy resolves sender/receiver against the live tab list and refuses the parse when a name is unknown, which a tailer cannot replicate **[V]**. **Bias to false-negative on "is public chat"** — drop ambiguous lines so private text never enters the corpus. The exact post-Via 2b2t public prefix shape (rank prefixes? does it literally match `<$s> $m`?) is **[?]**; log the first 20 dropped-line *shapes* (digit/letter-masked, never verbatim) at startup so the operator can tune the regex without ever printing chat.

### 8.4 Rotation — must be handled on every proxy restart

`StartupSizeAndTimeBasedTriggeringPolicy` forces `isTriggeringEvent()` to return true on the **first logging event of each process**, so `latest.log` is rolled into `log/proxy-yyyy-MM-dd.N.log.zip` and recreated at **every proxy start** **[V]** — on top of daily and 250 MB rollovers. An inode-pinned tail goes silently dead.

Poll every `log_tail.poll_interval_s` (default 1.0):
- `os.stat(path)` → `(st_dev, st_ino, st_size)`.
- If identity changed **or** size shrank: read the old handle to EOF, close it, reopen from **offset 0**, reset the offset in state.
- Never pin the inode. Persist `(st_dev, st_ino, offset)` in `state["log_tail"]` after every fsync so a restart resumes mid-file.
- Read only up to the last complete `\n`; keep the partial tail in memory.

Archive backfill (`log/proxy-*.log.zip`, one entry per zip, same line format **[V]**) is **out of scope for the collector** — ship it as a separate one-shot `import_proxy_zips.py`. Archives are deletable out from under you: logback prunes on start (`cleanHistoryOnStart=true`, `maxHistory=1000`, `totalSizeCap=250MB`) and `Proxy.clearOldLogs()` deletes every zip 10 s after startup if `CONFIG.debug.clearOldLogs` is ever turned on (default false) **[V]**. Copy them out promptly if you want them.

### 8.5 Marking and reconciliation against SSE rows

Log rows are written into the same stream but are **structurally distinguishable and never authoritative**:

- `src = "proxy-log"`, `precision = "s"`, `player_uuid = null`, `component = null`, `row_id` prefixed `"L"`.
- `ts` = the log's local timestamp converted to UTC using `log_tail.timezone` (config; default the system zone) with `ts_us` floored to the whole second. **Flag DST ambiguity:** if the local time is ambiguous (fall-back hour), pick the earlier interpretation and increment `log_tz_ambiguous`. The log carries no offset **[V]**, so this cannot be resolved from the line.
- Log rows **never** advance `hwm_us`, never create or close gaps, and never enter the SSE/backfill dedupe set. They are an independent stream sharing a file.

**Offline reconciliation** (`reconcile_log.py`, run on demand, not by the collector). For each `proxy-log` row, look for an `sse`/`rest-backfill` row with the same `chat`, the same `player_name`, and `|ts_us_log - ts_us_sse| <= 2_000_000`. Report three buckets:

- **matched** — the normal case. Proves both paths saw the message; no action.
- **log-only** — a message the SSE feed never delivered. This is the valuable output: direct evidence about the feed-vs-DB unknown of §7.8 and about the publisher's filtering **[?]**.
- **sse-only** — expected in bulk whenever the bot was in queue or offline, or when the message was a form the whitelist dropped. Not a defect by itself.

The ±2 s tolerance absorbs second-flooring plus proxy/API ingest skew. Do **not** attempt to merge matched rows in place — the corpus keeps both, and the mining pipeline filters `src == "sse" or src == "rest-backfill"` by default. `precision` and `src` on every row are exactly what make that filter possible.

**Do not use `debug.log`** (millisecond timestamps, gated on `CONFIG.debug.debugLogs`) **[V]** — much noisier for one extra digit of precision that still cannot bridge to a microsecond key. **Do not build on `ChatHistory`** (in-memory, 10 messages, 30 s, default off) or the Discord `chatRelay` **[V]**. And if UUID fidelity in a proxy-side capture ever becomes a real requirement, the answer is not a better tailer — it is enabling the existing `ChatDatabase`, which already writes `(time, chat, player_name, player_uuid)` per `PublicChatEvent`; the cost is `CONFIG.database.enabled=true` + credentials, a Redis/Redisson instance for the cross-instance lock, and hand-creating the `chats` table because the proxy ships no DDL for it **[V]**.

---

## 9. Configuration surface

One JSON file (`collector.json`), every key overridable by env var `CHATCOL_<UPPER_SNAKE>`. Validate at startup and **exit non-zero on any unknown key** — a typo'd key that silently keeps a default is how a months-long unattended run ends up misconfigured.

```json
{
  "data_dir": "./corpus",
  "state_path": "./corpus/state.json",
  "status_path": "./corpus/status.json",
  "log_path": "./corpus/collector.log",
  "lock_path": "./corpus/collector.lock",
  "user_agent": "chat-corpus-collector/1.0 (+contact: <operator>)",

  "sse": {
    "enabled": true,
    "host": "api.2b2t.vc",
    "path": "/feed/chats",
    "socket_read_timeout_s": 20,
    "heartbeat_deadline_s": 90,
    "initial_grace_s": 120,
    "backoff_reset_s": 60,
    "backoff_broken_max_s": 60,
    "backoff_429_max_s": 300
  },

  "backfill": {
    "enabled": true,
    "host": "api.2b2t.vc",
    "path": "/chats/window",
    "page_size": 100,
    "min_interval_s": 1.0,
    "request_timeout_s": 30,
    "safety_lag_s": 120,
    "second_pass_delay_s": 5400,
    "max_lookback_days": 7,
    "backoff_429_max_s": 300,
    "audit_enabled": true,
    "audit_interval_days": 7,
    "audit_span_days": 7,
    "audit_lag_days": 1
  },

  "log_tail": {
    "enabled": false,
    "path": "C:/path/to/bot/log/latest.log",
    "poll_interval_s": 1.0,
    "timezone": null,
    "schema_from_config_path": null
  },

  "storage": {
    "store_component": true,
    "fsync_interval_ms": 2000,
    "gzip_delay_s": 3600,
    "gzip_level": 6,
    "max_file_bytes": 0
  },

  "dedupe": {
    "window_s": 21600,
    "max_keys": 400000,
    "rehydrate_max_bytes": 268435456
  },

  "ops": {
    "status_interval_s": 15,
    "alert_command": null,
    "log_level": "INFO",
    "log_max_bytes": 10485760,
    "log_backup_count": 5
  }
}
```

Notes on two of these:
- `storage.store_component`: `component` is available **nowhere else** — absent from the `chats` table, from `chats.csv`, and from every REST response **[V]**. Dropping it at ingest loses the raw Minecraft component permanently. It also accounts for roughly **85% of the on-disk bytes** (§11.1). Default `true`; the operator can flip it if disk matters more than fidelity. Whether it is ever `null` or empty is **[?]** — the schema allows null and the writer must pass it through unchanged.
- `log_tail.timezone`: an IANA name resolved via `zoneinfo` (stdlib, 3.9+). On Windows `zoneinfo` needs the `tzdata` package, which would violate the no-dependencies rule — so if `zoneinfo.ZoneInfo(name)` raises, fall back to a fixed offset supplied as `"+HH:MM"` and refuse to start on anything else.

---

## 10. State file and crash safety

`state.json`:

```json
{
  "version": 1,
  "hwm_us": 1785012433404045,
  "seq": 184223,
  "connections": 417,
  "active_file": "chat-2026-07-25.jsonl",
  "pending_gzip": [],
  "gaps": [ ... ],
  "log_tail": {"dev": 3, "ino": 8796093022208, "offset": 1048576, "path": "..."},
  "audit_last_run_us": 1784900000000000,
  "counters": {"rows_sse": 0, "rows_backfill": 0, "rows_log": 0,
               "dupes_dropped": 0, "parse_errors": 0, "tie_skips": 0,
               "clamped_gaps": 0, "rows_added_pass2": 0, "audit_rows_added": 0}
}
```

### Atomic write

```python
def save_state(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(json.dumps(obj, indent=1, sort_keys=True).encode())
        f.flush(); os.fsync(f.fileno())
    if os.path.exists(path):
        try: os.replace(path, path + ".bak")
        except OSError: pass
    os.replace(tmp, path)
    try:
        d = os.open(os.path.dirname(path) or ".", os.O_RDONLY); os.fsync(d); os.close(d)
    except OSError:
        pass                      # directory fsync is unavailable on Windows
```

`os.replace` is atomic on both POSIX and Windows (`MoveFileEx` + `REPLACE_EXISTING`). On load: try `state.json`; on `JSONDecodeError` or missing, try `state.json.bak` and log at ERROR; if both fail, treat as first run (which means **no backfill**, `hwm_us = now` — §7.9).

### Ordering guarantee

```
append record bytes  →  (fsync if interval elapsed)  →  update in-memory state
...
before ANY save_state():  fsync the active data file first
```

`state.json` is thus always at or behind the durable data. After a hard kill the collector re-fetches the gap `[hwm_us - 120s, reconnect]`, the dedupe set rehydrated from the data file discards everything already written, and the net effect is zero loss and zero duplicates.

State is persisted: after every backfill page, on every gap enqueue/complete, on every rotation step, and at most every 10 s during steady SSE flow (the SSE HWM does not need to be persisted per-row — the 120 s `safety_lag` already covers far more than 10 s of drift).

### Single-instance lock

`open(lock_path, "a+")`, then `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on POSIX / `msvcrt.locking(fd, LK_NBLCK, 1)` on Windows. Both stdlib. Write the PID into the file for the operator's benefit. On failure: print the holding PID and exit 3. **Two collectors on one IP would also double the `/feed/*` connection count against the cap of 6** **[V]** — this lock is a correctness requirement, not hygiene.

---

## 11. Operations

### 11.1 Disk growth

Anchored on rfresh's 2025 bulk dump: **6,701,781 chat rows/year**, `chats.csv` = 775,865,797 B uncompressed (115.8 B/row) / 172,660,076 B deflate (25.8 B/row) for the same four fields we store **[V]**. Everything below is *derived*, not measured — instrument it and re-check after week one.

| | per row | per day (~18.4k rows) | per month (~558k) | per year |
|---|---|---|---|---|
| JSONL, `store_component: false` | ~200 B | ~3.7 MB | ~112 MB | ~1.3 GB |
| gzipped | ~28–35 B | — | ~16–20 MB | ~190–235 GB → **~0.2 GB** |
| JSONL, `store_component: true` | ~730 B | ~13 MB | ~408 MB | ~4.9 GB |
| gzipped | ~55–75 B | — | ~31–42 MB | **~0.4–0.5 GB** |

`component` is 495–532 chars in the two samples observed **[V]** and dominates the record. It is also extremely repetitive JSON, so it compresses far better than the rest — hence the modest gzipped delta. **Recommendation: keep `store_component: true`.** Half a gigabyte per year is nothing against permanently losing the only copy of the raw component.

Steady state with component on: **~0.5 GB/year on disk**, plus one uncompressed day (~13 MB) at any instant. Provision 5 GB and forget about it. Volume is bursty and peak-hour rates were never measured **[?]** — the observed ~3/min quiet-window rate **[V]** may be well below peak, so treat the table as a floor and let `status.json` tell the truth.

### 11.2 Restart behaviour

1. Acquire the lock; exit 3 if held.
2. Load state (or `.bak`); if neither exists → first run, `hwm_us = now`, **no backfill**.
3. Delete stray `*.gz.tmp`; re-drive `pending_gzip`.
4. Repair the tail of the active file (§5.4).
5. Rehydrate the dedupe set from the corpus, anchored on `hwm_us` (§4.3b).
6. Enqueue a startup gap `[hwm_us - safety_lag, now + 5s]`, clamped to `max_lookback_days`; persist.
7. Start the backfill thread, then connect the SSE stream.

The operator's expectation: a restart costs nothing but a handful of re-fetched rows, and any downtime up to 7 days heals automatically. Downtime beyond 7 days requires manually enqueuing a gap (ship `enqueue_gap.py <start_iso> <end_iso>` for exactly this) — the clamp exists so a bad `hwm_us` can never trigger a 2013-onward crawl **[V]**.

Run it under a supervisor that restarts unconditionally: `systemd` with `Restart=always RestartSec=5`, or NSSM on Windows. The process is designed to be killed.

### 11.3 Health checking

`status.json`, rewritten atomically every 15 s:

```json
{
  "ts": "2026-07-25T20:41:02.113000Z",
  "pid": 4812,
  "uptime_s": 918233,
  "sse": {"state": "connected", "connections": 417,
          "s_since_last_inbound": 12.4, "s_since_last_event": 41.7,
          "heartbeats": 15302, "reconnects_last_hour": 4,
          "last_close_mode": "CLEAN_EOF"},
  "backfill": {"gaps_pending": 0, "gaps_error": 0, "gaps_running": 0,
               "last_429_s_ago": 3611, "rows_added_pass2": 12,
               "audit_rows_added": 0, "audit_last_run": "2026-07-20T03:00:00.000000Z"},
  "log_tail": {"enabled": false},
  "storage": {"active_file": "chat-2026-07-25.jsonl", "active_bytes": 9114233,
              "rows_today": 12488, "pending_gzip": 0, "disk_free_bytes": 41231237120},
  "counters": { ... },
  "health": "ok"
}
```

`collector.py --health` reads `status.json` and exits:

- **0 (ok)** — `status.json` newer than 60 s; SSE `connected`; `s_since_last_inbound < 90`; `gaps_error == 0`; `pending_gzip <= 2`; `disk_free > 2 GiB`.
- **1 (warn)** — `reconnects_last_hour > 20`; or `gaps_pending > 10`; or `dedupe_evictions > 0`; or `tie_skips > 0`; or `parse_errors` rising; or `disk_free < 5 GiB`.
- **2 (critical)** — `status.json` stale > 120 s (process wedged or dead); or `gaps_error > 0`; or `clamped_gaps > 0`; or `disk_free < 2 GiB`.

`ops.alert_command` is invoked via `subprocess.run` on every health-state *transition* only (never repeatedly), with the state name as `argv[1]` and a one-line reason as `argv[2]`. That is where the operator wires ntfy. **The reason string must never contain chat text** (§11.4).

What "healthy" looks like, so the operator does not chase normal behaviour:
- **~96+ reconnects/day is normal** — the 15-minute server-side stream cap **[V]**. Only the *rate* matters.
- `s_since_last_event` over 60 s is normal; over 300 s in a quiet window is plausible **[?]** — the watchdog deliberately does not use it.
- Occasional 429 on backfill is normal; ~18% failure at 3–4 s pacing was measured **[V]**.

Quick manual check, no tooling:
```bash
python collector.py --health; echo $?
zcat corpus/chat-2026-07-2*.jsonl.gz | wc -l          # rows per closed day
tail -c 2000 corpus/chat-$(date -u +%F).jsonl | tail -1 | python -m json.tool
```

### 11.4 Privacy and log hygiene — hard rule

The corpus contains raw 2b2t public chat, which routinely contains coordinates. Per the operator's standing order, **no real 2b2t coordinates may ever appear in a log, a status file, an alert, or a terminal echo.** Therefore:

- `collector.log` and `status.json` **never** contain a `chat` value, a `component` value, or any record body — not on parse errors, not on dedupe drops, not on validation failures. Log counters, `row_id`, `ts`, and `player_uuid` only.
- The dropped-line-shape diagnostic in §8.3 emits **digit/letter-masked** shapes only.
- `alert_command` receives counters and state names, never content.
- Chat content lives in exactly one place: the JSONL files.

---

## 12. Where the reports disagree or left something open

Each item states the ruling the implementer must follow. Do not re-litigate these; do not go looking for more information.

**12.1 — When a fresh gap may be backfilled. (Direct conflict.)**
`sse` concludes from the commented-out `scraperTimeCutoff = now - 1h` guard that recent DB data may be incomplete and that a just-occurred gap should be re-queried an hour later rather than immediately **[V code, [L] inference]**. `backfill` verified empirically that `/chats/window` reads Postgres directly (not the 3-min DuckDB replica) and returned a row 32 s behind wall clock **[V]**.
→ **Do both** (§7.7): pass 1 immediately for fast recovery, pass 2 over the identical range at `end + 90 min`. Report `rows_added_pass2` so the ambiguity resolves itself with data instead of argument. Do not choose one report over the other.

**12.2 — `precision` for backfilled rows. (Direct conflict.)**
`schema` says set `precision = "ms"` for `rest-backfill` because `/dump/player` truncates to milliseconds **[V]**. `backfill` verified `/chats/window` returns genuine microseconds (digit histogram `{6:182, 5:17, 4:1}`) **[V]**.
→ Both are right about different endpoints. **This spec never uses `/dump/player`** (it is a melted 3-column `time,data,table_name` shape with descending interleaved sort, no identity columns, and millisecond truncation **[V]** — unusable and unnecessary). `rest-backfill` rows are `precision = "us"`. If anyone later adds a `/dump/player` path, it must use a distinct `src` value and `precision = "ms"`.

**12.3 — `precision` for proxy-log rows. (`schema` is simply wrong here.)**
`schema` lists `proxy-log` under `precision = "us"`. `proxylog` verified the file pattern is `HH:mm:ss` — **whole seconds** **[V]**.
→ `proxy-log` is `precision = "s"`. This is not cosmetic: it is why log rows cannot share a dedupe namespace with SSE rows and need the fuzzy ±2 s reconciliation of §8.5.

**12.4 — Dedupe key granularity and the null-UUID fallback. (Conflict.)**
`schema` recommends hashing at **millisecond**-truncated time with a `"@null"` sentinel, specifically so an SSE (µs) row collapses with a `/dump/player` (ms) row. `backfill` recommends full **microsecond** integer time with a `"name:" + playerName` fallback.
→ **Follow `backfill`.** Millisecond truncation only buys anything if a millisecond-precision source exists, and per 12.2 none does; meanwhile it doubles the false-merge surface. The `name:` fallback is strictly more informative than `@null` and `player_name` is NOT NULL **[V]**. Note the residual: two genuinely distinct messages from the same player with identical text inside the same microsecond would merge — impossible for a single client the scraper records as one insert **[V]**.

**12.5 — Shape of an app-side 429. (Conflict.)**
`sse` says the resilience4j 429 carries a plain-text body `"Too many requests"` from `CustomExceptionHandler` **[V, from source]**, and suggests distinguishing it from the edge's `text/html` 429. `backfill` observed live 429s with **no `Content-Type` and a zero-length body** **[V, from probes]**.
→ **Never branch behaviour on 429 body or content-type.** Log both for diagnosis; treat every 429 as transient with the backoff of §7.6 (REST) or §6.4 (SSE). Whether the sustained 429 periods come from the in-process bucket or an additional Cloudflare per-IP rule is **[?]**, and the practical impact is identical either way.

**12.6 — The proxy-log line format was never observed on a real chat line. [?]**
`proxylog` derived it from `logback.xml` plus the component serializer; the local dev `latest.log` had one line and none of ~30 archived zips contained a `[Chat]` match **[V, as a limitation]**. Also unconfirmed: the exact post-Via 2b2t public-chat prefix shape (rank prefixes? does it literally match `<$s> $m`?), whether embedded newlines are escaped **[L: they are not]**, and whether the deployed fleet actually runs default `logChatMessages` / `logOnlyQueuePositionUpdates` values.
→ Log-tail stays **off by default**. When enabled, run it in `log_tail.dryrun` mode first: parse, classify, write nothing, and report masked line-shape counts. Only turn on writing once the operator has confirmed the whitelist matches reality. Bias every ambiguous line to *drop*.

**12.7 — Whether the SSE set equals the DB set. [?]**
The feed comes from a Redis reliable topic published by an external fleet; the Postgres table is a separate ingest path; there is no code-level guarantee in either direction **[V]**. Whether the publisher filters server broadcasts, whispers, command output or duplicate bot sessions is **[?]** (it lives in ZenithProxy, not the API repo).
→ Defence: the weekly completeness audit of §7.8, on by default, plus `audit_rows_added` in `status.json`. This is the single most valuable defensive measure in the spec, because it is the only one that can detect *systematic* loss rather than *episodic* loss.

**12.8 — Out-of-order delivery is structurally possible but never observed. [V structurally / [?] in practice]**
Virtual-thread-per-message fan-out means no sequencing **[V]**; no inversion was captured in ~4 minutes of low-traffic probing **[?]**.
→ Defences already built in: `safety_lag_s = 120` on every gap start (§6.5), `seq` on every row, and never sorting in the hot path. Do not tighten `safety_lag_s` on the grounds that inversions "probably don't happen."

**12.9 — Whether the hourly Redis listener refresh replays, drops, or is seamless. [?]**
`sse` rates a replay window as **[L]**; it depends on Redisson `RReliableTopic` subscriber-position semantics across `removeListener`/`addListener` and was not observed.
→ `dedupe.window_s = 21600` (6 h) is deliberately much wider than 1 h so a replay is absorbed regardless. If it *drops* rather than replays, the hole is invisible to gap detection (no disconnect occurs) — which is, again, what the weekly audit catches. Do not shrink the dedupe window below 2 h.

**12.10 — Whether `component` can be null or empty, and whether its schema is stable. [?]**
Only two same-shaped player-chat components were sampled **[V]**. It uses the **modern snake_case** schema (`click_event` / `hover_event`, not camelCase) **[V]**, which will drift as 2b2t's Minecraft version moves.
→ Store the raw string **verbatim and unvalidated**; allow `null`; never let a component parse failure affect ingest. The collector must not decode it at all. Any component walking happens offline, at mining time, where a schema change costs a re-run instead of a data gap.

**12.11 — Fractional-digit width, and precision-era transitions. [V variable / [?] exact dates]**
Widths of 0–9 are possible across sources; the eras (whole seconds in 2013, ms in 2023, µs in 2026) were sampled but the transitions were not **[?]**.
→ The regex parser of §3.1 handles 0–9 digits uniformly with floor-truncation to 6. Never assume a width, never slice at a fixed offset, never rely on `fromisoformat`.

**12.12 — Whether the 6-connection cap is per IP, per /24, or per colo; whether it escalates on repeated breach; whether an API key raises it. [?]**
The app has no key handling at all — any accommodation is an edge-side change by the maintainer **[V]**.
→ Hold exactly **one** connection, enforce the single-instance lock, and back off 60–300 s on 429. Never work around the cap with multiple IPs; if more streams are ever genuinely needed, ask in the support Discord linked in the API docs.

**12.13 — Deep-offset behaviour beyond ~6900, and tie-group page boundaries. [?]**
→ Sidestepped entirely by time-slicing (§7.3). The `page` parameter is never sent. The tie-group guard (§7.4) covers the one residual case.

**12.14 — The commented-out "Migrate your scraping to /feed/chats" guard could be enabled at any time. [V that it exists]**
It would 400 any window request anchored within 1 h of now.
→ Never build polling of recent data as a primary or fallback strategy. The SSE feed is the sanctioned live path. If pass-1 backfill starts returning 400 with that message, the collector's 400 handling (§7.5) marks the gap `error` and alarms — at which point the operator changes `second_pass_delay_s` semantics to make pass 1 wait out the hour. Note this in the runbook so the failure is diagnosable in one minute instead of one afternoon.

---

## 13. Suggested module layout and build order

```
collector.py        entry point, config load, lock, supervisor loop
tsutil.py           parse_ts / fmt_ts / fmt_bound            ← build & unit-test FIRST
record.py           canonical record, row_id, from_sse, from_window, from_logline
writer.py           append, fsync, rotate, gzip, repair, dedupe set, state
statefile.py        atomic save/load
ssereader.py        connection, frame parser, watchdog, backoff, gap emission
backfiller.py       gap queue, time-slice paging, 429 handling, audit job
logtail.py          rotation-aware tail, schema whitelist  (optional, phase 2)
status.py           status.json, --health, alert_command
tools/export_csv.py           dump-compatible CSV
tools/dedupe_report.py        corpus-wide duplicate audit
tools/reconcile_log.py        proxy-log vs SSE reconciliation
tools/enqueue_gap.py          manual gap injection
tools/import_proxy_zips.py    proxy archive backfill
```

Order: `tsutil` + `record` with unit tests (the timestamp rules are where a silent corpus-wide bug would live) → `writer` + `statefile` with a kill-9 test harness → `ssereader` → `backfiller` → run it for a week with `log_tail` off → then `logtail` in dryrun.

**Two tests are non-negotiable before this runs unattended:**
1. **Kill-9 loop.** Drive synthetic records through the writer, `SIGKILL` the process at random points 200 times, and after each restart assert: the active file's every line parses as JSON, no `row_id` appears twice, and no record acknowledged before the kill is missing.
2. **Boundary-row test.** Feed a fake `/chats/window` server whose inclusive lower bound re-delivers the boundary row, across a simulated 3-day downtime, and assert the boundary row is written exactly once — this exercises the `hwm_us`-anchored rehydration of §4.3b, which is the mechanism the whole no-duplicate/no-drop guarantee rests on.