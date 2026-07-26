# External APIs

Everything here was verified live on 2026-07-25. Each entry is recorded because it is
counter-intuitive, undocumented, or contradicts what the obvious approach would assume — the
things worth re-deriving from scratch are not in this file.

---

## api.2b2t.vc

Open source ([rfresh2/2b2t.vc-api](https://github.com/rfresh2/2b2t.vc-api)), OpenAPI 3.1 spec
at `/v3/api-docs`, **no authentication**.

Endpoints the app touches: `/seen`, `/stats/player`, `/deaths`, `/kills`, `/chats`,
`/chats/window`, `/connections`, `/dump/player`, `/tablist/info`, and the SSE `/feed/chats`.

### The rate limit is global, not per-IP

`/chats/window` and its siblings sit in a resilience4j bucket called `main` that allows
**5 permits per second across every caller on the internet**, not 5 per client. Your effective
throughput therefore depends on strangers' traffic, and a burst you did not cause can 429 you.

**429s carry no `Retry-After`.** You have to pick your own backoff. Roughly 18% of requests
were observed 429ing even while pacing at one request every 3–4 seconds, with saturated
periods lasting 75+ seconds. Pace at ~1/s and treat 429 as normal weather, not an error.

`/feed/*` is in **no bucket at all** — the SSE stream costs zero permits — but the edge caps
it at **6 concurrent connections per IP, shared across all three feeds**. Leaked sockets
manufacture their own failures here, which is why the collector enforces single-instance
locking.

### Two calls that save hundreds

- **`/stats/player`** returns exact corpus counts in one request. Use it to decide whether
  pagination is even worth starting.
- **`/dump/player`** returns an entire player history as a single CSV. Server-wide
  concurrency is **1**, so it queues globally — but above roughly 2,000 chat rows it beats
  paginating by a wide margin.

### `/chats/window` bounds are both inclusive

Stitched windows therefore **re-deliver the boundary row every time, by design**. The
intuitive fix — advancing the next window's start by an epsilon — provably drops rows: a real
event at `…872508` vanishes when `startDate` is rounded to `…873`. Dedupe on the boundary
row instead of trying to avoid it. Steadily climbing duplicate counts are the system working.

### A bad date returns 302, not 400

An unparseable date **redirects to the API explorer page**. With redirect-following enabled
this looks like a successful, permanently empty poll — silent data loss that no error counter
catches. Disable redirects and reject non-JSON content types so it surfaces loudly.

### SSE wire format

No `event:`, no `id:`, no `retry:`. Frames are exactly `data:{json}\n\n`. A `:heartbeat`
comment arrives every 60.000s **on a global schedule**, so the first heartbeat after connect
can take up to 60s depending on arbitrary phase. Connections have a **15-minute server-side
lifetime** that presents as a clean EOF, so ~96 reconnects/day is normal and healthy. There
is no `Last-Event-ID`, so **replay is impossible** — every disconnect is a hole that must be
healed through the REST endpoint.

Payload is `{time, chat, playerName, playerUuid, component}`. The raw Minecraft `component`
exists **nowhere else** — not in the `chats` table, not in the bulk CSV, not in any REST
response.

> Field-name trap: it is `playerUuid`, not `uuid`. Getting this wrong yields rows that look
> fine but carry null UUIDs, which only shows up months later when you try to join on them.

### The 2025 bulk dump

`https://cdn.2b2t.vc/vcdb-2025.zip` — 468 MB zipped, 2.08 GB raw, 7 CSVs:

| file | rows |
|---|---|
| chats | 6,701,781 |
| connections | 10,132,709 |
| deaths | 2,894,540 |
| queue lengths | 107,453 |
| player counts | 105,396 |
| queue waits | 2,506 |
| restarts | 53 |

There is **no `kills.csv`** — kills derive from `deaths` by killer UUID.

Calendar 2025 only, frozen 2026-01-06, which makes it **useless for the live path**: recent
deaths and recent chat are 2026 data. Its value is entirely offline — tuning keyword patterns
and establishing profanity base rates against 6.7M real lines without spending a single rate
limit permit. Query the CSVs directly with DuckDB; convert to Parquet if you will query
repeatedly.

---

## Minecraft identity

### Account creation date is not obtainable. At all.

Mojang only ever exposed it to the **authenticated owner** of the account. The `?at=`
binary-search trick that used to approximate it has been **silently ignored since November
2020** — it does not error, it just returns the current value, so code depending on it appears
to work. `/user/profiles/{uuid}/names` has **404'd since September 2022**.

Every third-party site still showing a "created" date is re-serving Ashcon's frozen
pre-2020 estimate; Ashcon's own documentation says so.

**Substitute 2b2t `firstSeen`.** For vetting on this server it is the more meaningful number
anyway, and unlike an account age it cannot be purchased.

### Name history

`laby.net/api/v3/user/{uuid}/names` is the only working source (~10 req/min, unofficial).

**NameMC is a dead end** — no public API, a Cloudflare challenge on server-side fetch, and a
robots.txt that names ClaudeBot and GPTBot explicitly. It holds nothing Laby doesn't.

---

## Discord

### Bots can search guild messages now

`GET /guilds/{id}/messages/search` went GA for bots on **2026-03-19**. `author_id` alone is a
valid query, which removes the entire reason to build a message-indexing subsystem.

Requires the **`MESSAGE_CONTENT` privileged intent** — note this gates content on the REST
response too, not only on the gateway. Result ceiling is ~10,000 per query; window by
snowflake to go past it.

### Interaction constraints that shape the design

- **3 seconds** to make an initial response, then a **15-minute** interaction token. The
  review card's five API calls take ~5s, so defer the interaction and run inline — no job
  queue needed.
- **Components V2** (`IS_COMPONENTS_V2`, `1 << 15`) is **irreversible once sent** on a
  message. Skip it for the pinned panel, which needs to stay editable for years.
- Guild ceilings: ~1,000 active threads and ~500 channels. Private threads per request are
  fine at kit volume; whether archiving a thread frees active-thread quota is a question
  Discord's own issue tracker leaves unresolved, so do not design a system whose correctness
  depends on the answer.
