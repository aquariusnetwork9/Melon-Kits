# chat-corpus

An unattended collector that archives 2b2t public chat into an append-only JSONL corpus.
It holds one Server-Sent-Events stream open against `api.2b2t.vc/feed/chats`, writes every
event to disk in a fixed 12-key schema, and heals every disconnect by re-querying the same
range through the REST `/chats/window` endpoint. One Python process, standard library only,
designed to be killed at any instant and restarted without losing or duplicating a row.

**Why the SSE feed and not a proxy plugin.** A plugin on an AquariusProxy/ZenithProxy bot
looks like the obvious source, but plugin jars in `plugins/` are **not loaded at all** under
the GraalVM native-image runtime — `PluginManager.preLoadPlugins()` checks
`ImageInfo.inImageRuntimeCode()` and returns — so a plugin-based collector silently collects
nothing on the linux/native channel, which is the failure mode that costs you a month of
data before anyone notices. The SSE feed is the maintainer's sanctioned live path, it costs
zero rate-limit permits, and it is the only source that carries both the `playerUuid` *and*
the raw Minecraft `component` (which exists nowhere else — not in the `chats` table, not in
the bulk `chats.csv`, not in any REST response). Log-tailing a bot remains available as an
optional, lower-fidelity redundancy source (`log_tail.enabled`, off by default): it is
name-only, second-resolution, and never authoritative.

Full behavioural spec: [`docs/SPEC.md`](docs/SPEC.md). Binding module API:
[`docs/INTERFACES.md`](docs/INTERFACES.md).

---

## Install

There is nothing to install.

- **Python 3.9 or newer.** 3.9 is the floor, not the target — it runs on anything newer.
- **Standard library only.** No pip, no virtualenv, no vendored packages, ever. That is a
  hard project rule, not a current state of affairs.
- Copy this directory to the box and run it. Modules are flat inside `chat-corpus/`; the
  tools in `tools/` bootstrap `sys.path` themselves, so `python tools/corpus_stats.py` works
  from here with no `PYTHONPATH`. Every path below is relative to `chat-corpus/`, not to the
  repository root.
- Windows and Linux both work (dev is Windows, deploy is Linux). The two POSIX-only calls —
  directory `fsync` and `fcntl.flock` — are guarded.

---

## Quickstart

```bash
# 1. a config with your contact string in the User-Agent (Cloudflare 403s a default UA,
#    and the maintainer deserves to know who is on the feed)
cat > collector.json <<'JSON'
{
  "data_dir": "./corpus",
  "user_agent": "chat-corpus-collector/1.0 (+contact: you@example.com)"
}
JSON

# 2. see the effective config, including any CHATCOL_* overrides. Any unknown key is a
#    startup failure by design, so this is also the config typo check.
python collector.py --config collector.json --print-config

# 3. run it in the foreground once and watch the log
python collector.py --config collector.json

# 4. from another shell: is it healthy?
python collector.py --config collector.json --health; echo $?

# 5. after an hour: is it actually collecting?
python tools/corpus_stats.py --config collector.json
```

Then put it under a supervisor that restarts unconditionally. The process is designed to be
killed:

```ini
# /etc/systemd/system/chat-collector.service
[Unit]
Description=2b2t chat corpus collector
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/melon-kits/chat-corpus
ExecStart=/usr/bin/python3 collector.py --config /opt/melon-kits/chat-corpus/collector.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

On Windows, NSSM with the same restart policy.

**Do not run two collectors against one IP.** The edge caps `/feed/*` at 6 connections per
IP and leaked sockets manufacture their own 429s, so the single-instance lock
(`corpus/collector.lock`) is a correctness requirement. A second process exits 3.

---

## What lands on disk

```
corpus/
  chat-2026-07-25.jsonl        active day, append-only, one JSON object per line
  chat-2026-07-24.jsonl.gz     closed days, gzipped after a grace period
  state.json                   high-water mark, seq, gap queue  (+ .bak, atomic writes)
  status.json                  rewritten every 15 s; what --health reads
  collector.log                rotating; counters and ids only, never chat
  collector.lock               single-instance lock, holds the PID
```

One line, wrapped here for readability, entirely synthetic:

```json
{"ts":"2026-07-25T20:37:13.404045Z","ts_us":1785011833404045,
 "player_uuid":"00000000-0000-4000-8000-000000000001","player_name":"ExamplePlayer",
 "chat":"hello","component":"{\"text\":\"\",\"extra\":[{\"text\":\"hello\"}]}",
 "src":"sse","precision":"us","row_id":"9f2c...","ingest_ts":"2026-07-25T20:37:13.612001Z",
 "seq":184223,"batch":"c417"}
```

Two things about the layout that look like bugs and are not:

- **Files are partitioned by ingest date, not event date.** A row backfilled today for an
  event two days ago lands in *today's* file. That is what keeps the writer append-only with
  exactly one writable file at any instant, and it is why a closed, gzipped day is never
  reopened. Sort by `ts_us` downstream; `src` and `ts` on every row make the reordering
  explicit.
- **Rows are in receive order, not chronological order.** The collector never sorts. Any
  consumer that needs time order sorts offline.

---

## Configuration

The full surface is [SPEC §9](docs/SPEC.md); it is one JSON file and every key is also
settable as `CHATCOL_<UPPER_SNAKE>` (nested paths joined by `_`, e.g.
`CHATCOL_BACKFILL_MIN_INTERVAL_S=2`). **An unknown key — in the file or in the environment —
is a hard startup failure.** That is deliberate: a typo'd key that silently keeps its
default is how a months-long unattended run ends up misconfigured.

The handful an operator actually touches:

| key | default | when you change it |
|---|---|---|
| `user_agent` | placeholder | **Always.** Put a real contact in it. |
| `data_dir` | `./corpus` | Point it at the volume with the space. |
| `state_path`, `status_path`, `log_path`, `lock_path` | under `./corpus` | Only if you split state off the data volume. |
| `storage.store_component` | `true` | Set `false` only if disk genuinely matters: `component` is ~85% of the bytes but compresses well, and dropping it destroys the only copy of the raw Minecraft component. Keep it on. |
| `backfill.min_interval_s` | `1.0` | Raise to 2–4 if you see sustained 429s. `/chats/window` sits in a **global** 5-permit/second bucket shared with every other caller, so your throughput depends on strangers' traffic. |
| `backfill.max_lookback_days` | `7` | The clamp that stops a corrupt high-water mark from crawling back to 2013. Downtime longer than this is healed with `tools/enqueue_gap.py`, not by raising this. |
| `backfill.audit_enabled` | `true` | Leave on. The weekly completeness audit is the only thing that can detect *systematic* loss (the SSE feed and the database are separate ingest paths with no code-level guarantee that either is a superset). |
| `ops.alert_command` | `null` | Where you wire ntfy. Called as `cmd <state> <reason>` on health-state **transitions** only. |
| `ops.log_level` | `INFO` | `DEBUG` while diagnosing. Still never logs chat. |
| `log_tail.enabled` | `false` | Redundancy from a proxy log on a box where the feed is unreachable. Read SPEC §8 first; run it in dryrun before it writes. |
| `dedupe.window_s` | `21600` (6 h) | Effectively never. Do not shrink below 2 h. |

---

## Runbook

### What healthy looks like

Most of what looks alarming here is normal, so check this list before chasing anything.

- **~96+ reconnects per day is NORMAL, and so is a `CLEAN_EOF` every ~15 minutes.** The API
  builds each stream with `spring.mvc.async.request-timeout=15m`; the timeout is mapped to a
  200 with a null body, which on an already-committed chunked response the client just sees
  as EOF. Extra unscheduled clean EOFs happen on every API deploy. `CLEAN_EOF` is logged at
  DEBUG, sleeps 0.5–2 s, and reconnects. **Never alert on reconnect count — only on rate**
  (`reconnects_last_hour > 20`).
- **`s_since_last_event` over 60 s is normal**; several minutes in a quiet window is
  plausible. The watchdog deliberately ignores it and judges liveness from inbound *frames*,
  including `:heartbeat` comments. The first heartbeat after connect can legitimately take up
  to ~60 s because the heartbeat scheduler is global and its phase relative to your connect
  is arbitrary.
- **Occasional 429s on backfill are normal.** ~18% of requests were observed 429ing even
  while pacing at one request every 3–4 seconds, with saturated periods lasting 75+ seconds.
  Backfill retries indefinitely with backoff; the gap queue is durable, so there is no
  deadline and nothing is lost. A 429 never delays an SSE reconnect — separate threads,
  separate clocks, and the SSE stream costs zero permits.
- **`gaps_pending` briefly non-zero after every reconnect is the design working**, not a
  problem. Every disconnect is a hole (there is no `Last-Event-ID`, so replay is impossible),
  so every reconnect enqueues `[hwm - 120 s, reconnect + 5 s]` and the worker closes it in
  seconds. Each gap is also re-walked once ~90 minutes later as a confirming second pass.
- **`counters.dupes_dropped` climbing steadily is correct.** Both `/chats/window` bounds are
  inclusive, so every page and every gap re-delivers its boundary row by design, and the
  dedupe key discards it.
- **`audit_rows_added` consistently 0 is the good outcome** — it means the live feed is a
  superset of the database in practice. A persistently non-zero value means the weekly audit
  is the only thing standing between you and a quietly lossy corpus; leave it enabled.

### Health checking

`status.json` is rewritten atomically every 15 s. `python collector.py --health` reads it and
exits:

| exit | state | conditions |
|---|---|---|
| **0** | ok | `status.json` newer than 60 s; SSE `connected`; `s_since_last_inbound < 90`; `gaps_error == 0`; `pending_gzip <= 2`; `disk_free > 2 GiB` |
| **1** | warn | `reconnects_last_hour > 20`; or `gaps_pending > 10`; or `dedupe_evictions > 0`; or `tie_skips > 0`; or `parse_errors` rising; or `disk_free < 5 GiB` |
| **2** | critical | `status.json` stale > 120 s (process wedged or dead); or `gaps_error > 0`; or `clamped_gaps > 0`; or `disk_free < 2 GiB` |

`ops.alert_command` fires on **transitions only**, with the state name as `argv[1]` and a
one-line reason as `argv[2]`. The reason is built from counters and state names and never
contains chat text.

Quick manual checks, no tooling:

```bash
python collector.py --health; echo $?
zcat corpus/chat-2026-07-2*.jsonl.gz | wc -l                 # rows per closed day
tail -c 2000 corpus/chat-$(date -u +%F).jsonl | tail -1 | python -m json.tool
python tools/corpus_stats.py                                 # the real answer
```

### Diagnosing the one that costs you an afternoon

**Symptom: backfill gaps go straight to `status: "error"`, `gaps_error > 0`, health flips to
critical, and it started happening on a day you changed nothing.**

The API source contains a commented-out guard that would 400 any `/chats/window` request
anchored within one hour of now, with a message about migrating your scraping to
`/feed/chats`. If the maintainer ever enables it, pass-1 backfill — which runs *immediately*
after every reconnect, by design — starts 400ing on every gap. A 400 is treated as a
permanent bug in our own request: the gap is marked `error`, the alert fires, and it is never
retried. That is correct behaviour for a real request bug and exactly wrong for this.

Diagnose it in one minute:

1. `grep -c 'HTTP 400' corpus/collector.log` — is the failure a 400, and is it on
   `/chats/window`?
2. If yes, rule out the three 400s that *are* our bug: `endDate <= startDate` (a zero-length
   gap; `enqueue_gap.py` refuses these, and the collector skips them), `pageSize > 100`, and
   a missing sort anchor. None of these appear spontaneously on a previously working
   collector.
3. Check the response body/log line for the migrate-to-`/feed/chats` message. If it is
   there, the guard is live: recent windows are now unqueryable, and the fix is to stop
   anchoring pass 1 inside the last hour — delay every gap's `not_before_us` by an hour so
   pass 1 behaves like pass 2. The SSE stream is unaffected and keeps collecting throughout;
   only the hole-healing is deferred.
4. A **302** instead of a 400 means something else entirely: an unparseable date. The API
   redirects to its own API-explorer page rather than erroring, so with redirect-following on
   this would look like a successful empty poll forever. Redirects are disabled and non-JSON
   content types are rejected precisely so this shows up as a loud error instead of silence.

### Restart and downtime

A restart costs a handful of re-fetched rows: the collector re-enqueues
`[hwm - safety_lag, now + 5 s]`, rehydrates its dedupe set from the corpus anchored on the
high-water mark, and discards everything it already has. **Downtime up to
`backfill.max_lookback_days` (7 days) heals automatically.**

Longer than that, the automatic gap is clamped, and you inject the range by hand:

```bash
systemctl stop chat-collector
python tools/enqueue_gap.py --config collector.json \
    --dry-run 2026-06-01T00:00:00Z 2026-06-19T00:00:00Z   # check it first
python tools/enqueue_gap.py --config collector.json \
    2026-06-01T00:00:00Z 2026-06-19T00:00:00Z
systemctl start chat-collector
```

The tool refuses to run while the collector holds the lock (two writers to `state.json` would
lose the gap), refuses a zero-or-negative span (it would 400), and refuses a span longer than
90 days unless you raise `--max-span-days` — a mistyped year is the one input that turns a
repair into a full-history crawl. Watch `backfill.gaps_pending` fall back to 0; a week of
chat is roughly 1,300 requests, so budget 20-plus minutes per week of gap at 1 req/s.

### Routine maintenance

| cadence | command | why |
|---|---|---|
| after the first hour, day, week | `python tools/corpus_stats.py` | confirms rows are landing on every day, in the right shape |
| monthly (cron) | `python tools/dedupe_report.py` | corpus-wide duplicate audit; exits 1 if it finds any |
| after any hard kill or restore | `python tools/dedupe_report.py` | the only check that sees the whole corpus at once |
| on demand | `python tools/export_csv.py --no-header >> chats.csv` | one continuous file with the bulk dump |

---

## Disk expectations

Anchored on the 2025 bulk dump: 6,701,781 rows/year, ~18.4k rows/day. Everything below is
derived, not measured — `status.json` and `corpus_stats.py` tell the truth after week one,
and volume is bursty with peak-hour rates unmeasured, so treat the table as a floor.

| | per row | per day | per month | per year |
|---|---|---|---|---|
| JSONL, `store_component: false` | ~200 B | ~3.7 MB | ~112 MB | ~1.3 GB |
| …gzipped | ~28–35 B | — | ~16–20 MB | **~0.2 GB** |
| JSONL, `store_component: true` | ~730 B | ~13 MB | ~408 MB | ~4.9 GB |
| …gzipped | ~55–75 B | — | ~31–42 MB | **~0.4–0.5 GB** |

Steady state with `component` on is **~0.5 GB/year**, plus one uncompressed day (~13 MB) at
any instant. **Provision 5 GB and forget about it.** Health warns under 5 GiB free and goes
critical under 2 GiB.

---

## Tools

All four are stdlib-only, stream rather than load, read `.jsonl` and `.jsonl.gz`
transparently, and have a `--help` that explains when you would reach for them.

| tool | what it does | exit codes |
|---|---|---|
| `tools/export_csv.py` | dump-compatible CSV: `time,chat,player_name,player_uuid`, LF, no BOM, RFC4180 quoting. `--no-header` to append to `chats.csv`; `--since`/`--until`; `--include-proxy-log` | 0 wrote rows · 1 nothing matched · 2 bad args |
| `tools/dedupe_report.py` | corpus-wide duplicate `row_id` audit in fixed memory (two passes over a 64 MiB bit sieve, exact results). Cron it monthly | 0 clean · 1 duplicates · 2 audit incomplete |
| `tools/enqueue_gap.py` | inject a backfill range into `state.json` for downtime past the 7-day clamp. Collector must be stopped | 0 queued · 1 write failed · 2 bad args · 3 collector running · 4 no state |
| `tools/corpus_stats.py` | rows per UTC day, rows by `src`, distinct players, ts range, null uuids, chat-length mean/median. `--json` | 0 counted rows · 1 empty corpus · 2 bad args |

Read `--help` on any of them for what to look for in the output — e.g. a climbing null-uuid
count means the `playerUuid`-vs-`uuid` field-name trap has bitten, and a day at a third of
its neighbours' row count is a hole worth feeding to `enqueue_gap.py`.

---

## Privacy

The corpus contains raw 2b2t public chat, which routinely contains coordinates. Per a
standing operator order:

- **Chat content lives in exactly one place: the JSONL files.** Nothing else.
- `collector.log`, `status.json`, and `alert_command` invocations **never** contain a `chat`
  value, a `component` value, or any record body — not on parse errors, not on dedupe drops,
  not on validation failures, not in a traceback. Diagnostics carry `row_id`, `ts`,
  `player_uuid`, counters, byte offsets and file basenames only. An exception message that
  interpolates a record body is treated as a defect, so exception text is built from
  metadata: field names, type names, and character counts.
- The tools follow the same rule. `corpus_stats.py` reports chat *lengths* and player
  *counts* and has deliberately no "top talkers" or "sample rows" mode; `dedupe_report.py`
  prints `row_id`/`ts`/`src`; the only tool that emits chat text at all is `export_csv.py`,
  whose entire job is writing it to a file you asked for.
- The optional log-tail's dropped-line diagnostic emits digit/letter-**masked** line shapes,
  never verbatim lines.
- Every example in this repo — README, docstrings, tests — uses synthetic text (`hello`,
  `gg`) and synthetic names and UUIDs. No real coordinates appear anywhere outside the
  corpus files themselves.
