# sse

2b2t.vc API Server-Sent-Events chat feed (GET https://api.2b2t.vc/feed/chats) — source-verified framing, lifetime, limits, payload and delivery semantics for a long-running consumer

## Findings

- **[verified]** Frames carry NO event name. The controller calls `emitter.send(msg, MediaType.APPLICATION_JSON)`, which emits only `data:{json}` followed by a blank line, so every event arrives as the default SSE type "message" (EventSource.onmessage). There is no `event:` line for any of the three feeds.
    - _evidence:_ repo src/main/java/vc/controller/FeedController.java, FeedHandler.messageConsumer(): `emitter.send(msg, MediaType.APPLICATION_JSON);` — no SseEmitter.event().name(...) builder anywhere; probe output line types were only `data:`, `:heartbeat`, and blank

- **[verified]** Wire format is exactly `data:{json}\n\n` — no space after the colon, LF line endings (no CRLF), one JSON object per event, terminated by one blank line.
    - _evidence:_ Probe of https://api.2b2t.vc/feed/chats: 1658 bytes, 6 LF, 0 CRLF; lines parsed as `data:` + valid JSON, each followed by an empty line

- **[verified]** There is NO `id:` field and no Last-Event-ID handling. Nothing in the code writes an id, and no handler reads the Last-Event-ID request header. Gaps therefore CANNOT be recovered from the stream — replay is impossible and a REST backfill is mandatory.
    - _evidence:_ src/main/java/vc/controller/FeedController.java (no .id() call, no @RequestHeader for Last-Event-ID); grep for 'Last-Event' across src/ returns nothing; probe saw no `id:` lines

- **[verified]** There is also no `retry:` field, so the server never suggests a reconnect delay. The consumer owns its own backoff policy (a browser EventSource would fall back to its ~3s default).
    - _evidence:_ Probe line-type histogram contained only `data:{`, `:heart`, and blank lines across 196s of stream

- **[verified]** A heartbeat IS sent: the SSE comment frame `:heartbeat\n\n`, emitted by a single @Scheduled task at a fixed rate of 1 minute that walks all three feeds' emitter maps.
    - _evidence:_ src/main/java/vc/controller/FeedController.java: `@Scheduled(fixedRateString = "1m") public void heartbeatActiveFeeds()` sending `SseEmitter.event().comment("heartbeat").build()` to deathsFeed, chatFeed and connectionsFeed emitters

- **[verified]** Heartbeat spacing is exactly 60.000s, but the phase relative to YOUR connect is arbitrary because the scheduler is global (server-wide), not per-connection. In a 196s probe heartbeats landed at t=16.0, 76.0, 136.0, 196.0 (gaps 59.999s, 60.000s, 60.000s) — the first one 16s after connect. A fresh connection can therefore wait up to ~60s for its first heartbeat.
    - _evidence:_ Timestamped probe (scratchpad/probe2.txt): heartbeats at 16.0/76.0/136.0/196.0s; gaps 59.999, 60.000, 60.000

- **[verified]** Max connection lifetime is 15 minutes. `new SseEmitter()` is constructed with no timeout argument, so it inherits the MVC async timeout, which the app sets to 15m. Periodic reconnect is NORMAL AND EXPECTED, roughly every 15 minutes.
    - _evidence:_ src/main/java/vc/controller/FeedController.java: `var emitter = new SseEmitter();` (no-arg) + src/main/resources/application.properties: `spring.mvc.async.request-timeout=15m`

- **[verified]** The 15-minute expiry looks like a CLEAN end-of-stream to the client, not an error: onTimeout removes the emitter and AsyncRequestTimeoutException is mapped to 200/null-body, but since the SSE response is long since committed the client simply observes EOF on the chunked body. A consumer must treat normal EOF as 'reconnect', not as success/termination.
    - _evidence:_ src/main/java/vc/translators/CustomExceptionHandler.java: `@ExceptionHandler(AsyncRequestTimeoutException.class) ... ResponseEntity.status(HttpStatus.OK).body(null)`; FeedController `emitter.onTimeout(() -> removeEmitter(emitterId))`

- **[verified]** There is no idle timeout distinct from the 15m cap, and the 60s heartbeat keeps intermediaries (Cloudflare, nginx) from idling the connection out. The controller also explicitly sets `X-Accel-Buffering: no` on all three feed responses so nginx does not buffer the stream.
    - _evidence:_ src/main/java/vc/controller/FeedController.java: `response.addHeader("X-Accel-Buffering", "no");` in chatFeedSSE/deathsFeedSSE/connectionsFeedSSE; no other timeout property in application.properties

- **[verified]** Streams are also closed cleanly on every API deploy/restart: a ContextClosedEvent listener calls emitter.complete() on all emitters and clears the maps. So unscheduled clean EOFs happen too, independent of the 15m clock.
    - _evidence:_ src/main/java/vc/config/FeedShutdownHook.java -> FeedController.shutdownFeeds() -> FeedHandler.shutdown() calling emitter.complete()

- **[verified]** The /feed/* endpoints are in NO resilience4j rate-limit bucket. FeedController carries no @RateLimiter annotation at all — unlike every data controller. So the main (5/s), cached (20/s) and queue (100/s) buckets are irrelevant to the feed, and an open SSE connection consumes ZERO rate-limit permits, neither continuously nor at connect.
    - _evidence:_ src/main/java/vc/controller/FeedController.java has no io.github.resilience4j import; repo-wide grep for @RateLimiter lists Bot/Chats/Connections/DataDump/Deaths/Playtime/PriorityPlayers/Queue/Seen/Stats/TabList/Time controllers only — FeedController is absent

- **[verified]** The real limit is a CONCURRENCY CAP OF 6 simultaneously open /feed/* connections per client IP, enforced at the edge, not a per-second request rate. Nine connects spaced 1.5s apart (far slower than any per-second bucket) yielded 200 for connections 1-6 and 429 for 7, 8, 9.
    - _evidence:_ Empirical: 9 sequential connects 1.5s apart, each held 45s -> conn1..conn6=200, conn7=conn8=conn9=429. Separately, 5 held connections + a 6th attempt after 80s of complete idle succeeded (6 total, under cap), disproving a windowed rate limit

- **[verified]** The 6-connection budget is SHARED across the three feed paths. With 6 streams open on /feed/chats, a 7th connection to /feed/deaths was rejected with 429.
    - _evidence:_ Empirical: 6x /feed/chats held open (1.5s apart) then GET /feed/deaths -> HTTP 429

- **[verified]** Over-cap rejection is immediate and edge-generated: HTTP 429 in ~0.12s with `Content-Type: text/html`, `Server: cloudflare`, and NO `Retry-After` header. It is not the app's own 429 (which would be text/plain body "Too many requests" from CustomExceptionHandler), so the consumer cannot distinguish it from a 429 by body type alone without inspecting content-type.
    - _evidence:_ Probe response headers on the over-cap attempt: `HTTP/1.1 429 Too Many Requests`, `Content-Type: text/html`, `Server: cloudflare`, no Retry-After; compare src/main/java/vc/translators/CustomExceptionHandler.java which returns `.body("Too many requests")`

- **[likely]** Holding feed streams does not appear to starve ordinary REST calls: with 6 SSE streams open, GET /queue still returned 200 as a 7th concurrent request, suggesting the cap is scoped to the streaming feed paths rather than to all per-IP concurrency.
    - _evidence:_ Empirical: 6x /feed/chats held open, then GET https://api.2b2t.vc/queue -> 200 (cf-cache-status not captured, so a cached edge hit cannot be fully excluded)

- **[verified]** The application itself imposes no cap on emitters — FeedHandler stores them in an unbounded ConcurrentHashMap and only logs the count. All connection limiting is external (Cloudflare/nginx), and no API-key/authorization handling exists anywhere in the app, so a raised limit would have to be granted at the edge.
    - _evidence:_ src/main/java/vc/controller/FeedController.java FeedHandler(..., new ConcurrentHashMap<>(), ...) with LOGGER.info("Added {} emitter: {}"); repo-wide grep for apikey/x-api/authorization/bearer in src/ matches only the Swagger description string 'Support & API Keys: https://discord.gg/nJZrSaRKtb'

- **[verified]** The feed is NOT derived from database rows. Each LiveFeed subscribes to a Redisson RReliableTopic (topic name "ChatsTopic" for chats) published by rfresh's external ZenithProxy bot fleet; the Postgres CHATS table is written by a separate ingest path. There is therefore no code-level guarantee that the SSE set equals the set of rows that land in the DB, in either direction.
    - _evidence:_ src/main/java/vc/feed/LiveChat.java: `new MessageProcessor<>("ChatsTopic", ChatsFeedRecord.class)`; src/main/java/vc/feed/RedisClient.java `redissonClient.getReliableTopic(topicName)`; ChatsController queries `vc.data.dto.Tables.CHATS` via jOOQ/DuckDB, wholly separate code paths

- **[verified]** EVENTS CAN ARRIVE OUT OF ORDER. LiveFeed's topic listener submits each incoming message as its own task to a virtual-thread-per-task executor, so two messages published milliseconds apart are fanned out by concurrent threads with no sequencing. Consumers must not assume the `time` field is monotonically non-decreasing across the stream.
    - _evidence:_ src/main/java/vc/feed/LiveFeed.java initTopicListener(): `executor.execute(() -> { for (MessageConsumer c : messageConsumers) c.consume(msg); })` where executor is the `virtualThreadExecutor` bean = `Executors.newVirtualThreadPerTaskExecutor()` (src/main/java/vc/Application.java)

- **[likely]** DUPLICATES ARE POSSIBLE and nothing in the API de-duplicates. There is no id field, no seen-set, and once per hour each feed tears down and re-registers its Redis reliable-topic listener — a reliable topic resuming from a stored subscriber position across a remove/add cycle is a plausible replay window.
    - _evidence:_ src/main/java/vc/feed/LiveFeed.java: `@Scheduled(initialDelay = 1, fixedRate = 1, timeUnit = HOURS) private void refreshTopicListener()` calling topic.removeListener(topicListenerId) then initTopicListener(); no dedup logic anywhere in vc.feed or FeedController

- **[verified]** A send failure on one client's emitter calls completeWithError, which aborts that client's stream mid-body — an abrupt, non-clean disconnect that a consumer will see as a truncated/reset read rather than clean EOF.
    - _evidence:_ src/main/java/vc/controller/FeedController.java FeedHandler.messageConsumer(): `catch (Exception e) { emitter.completeWithError(e); }`

- **[likely]** /feed/chats carries player chat messages only, keyed to a player identity; server death messages are a separate feed (/feed/deaths, DeathsFeedRecord with deathMessage/victim/killer) and join-leave events another (/feed/connections, ConnectionsFeedRecord with a JOIN|LEAVE enum). Whether non-player server broadcasts or whispers ever appear on the chat feed cannot be determined from this repo because the publisher lives in ZenithProxy.
    - _evidence:_ src/main/java/vc/feed/dto/ChatsFeedRecord.java, DeathsFeedRecord.java, ConnectionsFeedRecord.java, enums/FeedConnectionType.java; publisher is external (topic names only referenced, never published in this repo)

- **[verified]** Chat payload field set is exactly five keys, serialized in declaration order: time, chat, playerName, playerUuid, component. Confirmed byte-for-byte against the live stream.
    - _evidence:_ src/main/java/vc/feed/dto/ChatsFeedRecord.java record(OffsetDateTime time, String chat, String playerName, UUID playerUuid, String component); live probe JSON keys parsed as ['time','chat','playerName','playerUuid','component'] on every event

- **[verified]** `time` is an ISO-8601 UTC instant with NANOSECOND precision and a trailing 'Z' — 9 fractional digits observed on every sampled event (shape: YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ, 30 chars). This breaks naive parsers: JS Date and pre-3.11 Python datetime.fromisoformat choke or truncate; Java OffsetDateTime/Instant.parse handles it.
    - _evidence:_ Live probe: time strings of length 30 ending in 'Z' with exactly 9 fractional digits on both sampled events; DTO type is java.time.OffsetDateTime serialized by Jackson (tools.jackson ObjectMapper)

- **[likely]** The fractional-second digit count should be treated as VARIABLE, not fixed at 9 — Jackson's java.time serializer trims trailing zeros, so an instant landing on a whole millisecond will emit fewer digits. Parse leniently rather than by fixed-width string slicing.
    - _evidence:_ Both sampled events had 9 digits (nanos), but the serializer is Jackson's default OffsetDateTime writer whose output width depends on the value; only 2 samples observed

- **[verified]** `chat` is the plain-text message BODY only — it does not include the player name, no `<name>` prefix, and no color codes. Verified structurally: the chat string neither starts with '<' nor contains the playerName value.
    - _evidence:_ Live probe assertion output: `chat startswith <: False | playerName in chat: False` on both events; chat lengths 68 and 40 chars

- **[verified]** `component` is a STRING containing DOUBLE-ENCODED JSON — the full Minecraft text component for the rendered chat line, which must be JSON-parsed a second time after parsing the SSE data object. It is not a plain-text field and not a nested JSON object.
    - _evidence:_ src/main/java/vc/feed/dto/ChatsFeedRecord.java declares `String component`; live probe: component string lengths 532 and 495 chars, which json.loads() into a dict

- **[verified]** The component uses the MODERN snake_case component schema (1.21.5+), not the legacy camelCase one. Observed shape: top-level {text:"" (empty), extra:[4 elements]} where the name element carries color + click_event{action, command} + hover_event{action, value:{text, extra:[...]}} with a nested multi-level extra tree, and the first extra element is the literal angle-bracket opener.
    - _evidence:_ Live probe key-shape dump: {"extra":[{"color","text"},{"color","click_event":{"action","command"},"hover_event":{"action","value":{"extra":[{"extra":[...]},...],"text"}},"text"},{"color","text"},{"color","text"}],"text":""} — note click_event/hover_event, not clickEvent/hoverEvent

- **[verified]** `playerUuid` is a standard dashed UUID string (8-4-4-4-12, 36 chars), not a compact hex form.
    - _evidence:_ Live probe: playerUuid length 36 with exactly 4 dashes on both events; DTO type java.util.UUID

- **[verified]** CODE-EVIDENT POLITENESS GUIDANCE: rfresh has written, and left commented out, a guard in ChatsController/DeathsController/ConnectionsController that rejects /chats/window, /deaths/window and /connections/window requests whose range extends past `now - 1 hour` with the message "Migrate your scraping to /feed/chats". The intent is explicit — the feed is the sanctioned path for live/recent data, and polling the window endpoints for the last hour is discouraged and could be blocked at any time without notice.
    - _evidence:_ src/main/java/vc/controller/ChatsController.java lines 198 and 206 (and ConnectionsController 177/185, DeathsController 188/196): `// return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(new MigrateToLiveFeedResponse("Migrate your scraping to /feed/chats"));` guarded by `var scraperTimeCutoff = LocalDateTime.now().minusHours(1);`

- **[likely]** The same `scraperTimeCutoff = now - 1 hour` constant signals the DB is only considered settled about an hour back, so a REST backfill of a very recent gap may return incomplete data. Backfill of a just-occurred gap is unreliable; older gaps are safe.
    - _evidence:_ src/main/java/vc/controller/ChatsController.java: `var scraperTimeCutoff = LocalDateTime.now().minusHours(1);` used solely to fence off queries for newer-than-1h data

- **[verified]** The backfill endpoint is GET /chats/window (sort ASC requires startDate, DESC requires endDate; ISO date-time params; pageSize 1-100, default 25; 204 when empty). It sits in the `main` bucket (5 req/s, 10s queue wait then 429) and is response-cached for 5 minutes, so re-issuing an IDENTICAL window query will return the cached page and will not pick up rows inserted in the meantime.
    - _evidence:_ src/main/java/vc/controller/ChatsController.java chatWindow(): `@RateLimiter(name = "main") @Cacheable("chatsWindow")` with pageSize>100 -> 400; application.properties `cache.ttl.default-ttl=5m` with no chatsWindow override; CacheConfig.java Caffeine expireAfterWrite from that TTL

- **[verified]** Backfilled /chats/window rows have a DIFFERENT and smaller field set than feed events: PlayerChat(playerName, uuid, time, chat) — note the key is `uuid`, not `playerUuid`, and there is NO `component` field. A consumer merging live and backfilled data must normalize both shapes and tolerate a missing component.
    - _evidence:_ src/main/java/vc/controller/ChatsController.java: `public record PlayerChat(String playerName, UUID uuid, OffsetDateTime time, String chat) {}` selected from CHATS.PLAYER_NAME, CHATS.PLAYER_UUID, CHATS.TIME, CHATS.CHAT

- **[verified]** Observed chat volume in a quiet window was low and bursty: 10 events in 196s (~3/min), inter-event gaps ranging 2.4s to 50.2s; a separate 62s probe saw 2 events and 1 heartbeat. A >60s silence on the data channel is entirely normal, which is precisely why liveness must be judged from heartbeats, not from data.
    - _evidence:_ scratchpad/probe2.txt: 10 DATA events at 37.2/50.6/100.7/105.7/112.0/140.0/142.3/170.3/176.5/185.8s, gaps 2.4-50.2s; first probe scratchpad/feed_raw.txt: 2 data events + 1 heartbeat in 62s

- **[verified]** Response headers on a successful connect are `Content-Type: text/event-stream` (no charset), `Transfer-Encoding: chunked`, `Connection: keep-alive`, `Server: cloudflare`, `cf-cache-status: DYNAMIC`, plus Vary on Origin/Access-Control-*. CORS is wide open (allowedOrigins "*", GET only, maxAge 300), so browser consumers work without proxying.
    - _evidence:_ Live probe headers (scratchpad/headers.txt); src/main/java/vc/config/WebConfig.java addCorsMappings: addMapping("/**").allowedOrigins("*").allowedMethods("GET").allowedHeaders("*").maxAge(300)

- **[verified]** Path matching is CASE-INSENSITIVE, so /feed/chats and /FEED/Chats both route. Not something to rely on, but it means a mis-cased URL will not fail loudly.
    - _evidence:_ src/main/java/vc/config/WebConfig.java configurePathMatch(): `patternParser.setCaseSensitive(false)`

- **[verified]** Repo state examined: shallow clone of rfresh2/2b2t.vc-api at commit 58755976b7b90e4c62d0cded6957bdc262ff086c (Fri Jul 24 2026), Spring Boot 4.1.0 / Redisson 4.6.1 / resilience4j 2.4.0, deployed behind Cloudflare with an nginx layer in the chain.
    - _evidence:_ git log -1 in the clone; pom.xml spring-boot-starter-parent 4.1.0, org.redisson:redisson 4.6.1, io.github.resilience4j:resilience4j-spring-boot4 2.4.0; X-Accel-Buffering header implies nginx, response Server: cloudflare

## Unknowns
- Whether the /feed/chats publisher (rfresh2/ZenithProxy, a separate repo) filters what it publishes — e.g. whether server broadcasts, /msg whispers, command output, or queue/restart announcements ever reach the chat topic, and whether messages from ignored/duplicate bot sessions are suppressed. Not answerable from the API repo; would require reading ZenithProxy or long-run observation against a known chat log.
- Whether the feed set is a strict superset, subset, or neither of the rows that land in the Postgres CHATS table. The two paths are independent (Redis topic vs DB ingest), so a message could in principle appear on one and not the other. Requires an empirical cross-check: capture feed events, wait >1h, then query /chats/window for the same interval and diff.
- The exact origin and configuration of the 6-connection cap (Cloudflare rate-limiting rule vs nginx limit_conn) and therefore whether it is 6 per IP, per /24, or per Cloudflare colo — and whether it varies by time of day or under load. Also unconfirmed whether an API key from the support Discord raises it, since the app has no key handling at all.
- Whether exceeding the cap repeatedly escalates to a longer-lived IP block. Testing was deliberately kept light; observed 429s cleared as soon as connections were released, but a sustained abuse pattern was not (and should not be) probed.
- Whether the hourly refreshTopicListener cycle actually replays already-delivered messages, drops messages published during the swap, or is seamless. This depends on Redisson RReliableTopic subscriber-position semantics across removeListener/addListener and was not observed (would need a >1h capture straddling the refresh, cross-checked against a second consumer).
- Whether the API runs more than one instance behind the load balancer. If it does, each instance is an independent topic subscriber, which would change duplicate/ordering behavior for consumers that reconnect and land on a different instance.
- Whether out-of-order delivery is observable in practice or merely possible in theory. The virtual-thread fan-out makes it structurally unguaranteed, but no inversion was captured in ~4 minutes of a low-traffic window; a high-traffic capture would be needed to measure the real inversion rate and typical magnitude.
- Whether the sampled ~3 chats/min is representative. Both probes ran in one quiet window; peak-hour and queue-flush volume, and therefore the burst rate a consumer must absorb, were not measured.
- Whether `component` ever arrives null or empty (e.g. for messages ingested without a captured component), and whether its schema is stable across Minecraft version bumps on 2b2t — only two same-shaped player-chat components were sampled.
- Whether Cloudflare or the origin imposes any total-bytes or total-duration ceiling below the 15m MVC timeout in some conditions; all observed streams were closed by the client's own --max-time, so the 15m expiry was inferred from source rather than observed end-to-end.

## Build implications
- Parse events as unnamed SSE messages: split the stream on blank lines, accept only lines beginning with `data:` (strip exactly the 5-char prefix, do NOT assume a space after the colon), and JSON-parse the remainder. Do not register a named event listener — there is no `event:` line, so a handler bound to "chat" or similar will silently receive nothing.
- Treat any line starting with `:` as a liveness signal, not data. Specifically `:heartbeat`. Do not attempt to JSON-parse it.
- Build the watchdog around a >90s no-heartbeat rule (60s nominal interval plus margin), NOT around data silence — 50s+ gaps between chats are routine in quiet periods, and the first heartbeat can legitimately arrive up to ~60s after connect, so start the watchdog clock with an initial grace period of ~90-120s rather than expecting an immediate heartbeat.
- Expect and design for a clean stream close roughly every 15 minutes (spring.mvc.async.request-timeout=15m). Reconnect on clean EOF as the normal path; do not log it as an error, do not treat it as the stream being 'done', and do not let it trip an alerting threshold.
- Distinguish three close modes and back off differently: clean EOF (15m timeout or deploy) -> reconnect almost immediately with small jitter; abrupt/truncated read (completeWithError, network) -> short exponential backoff; HTTP 429 -> long backoff, because there is no Retry-After header to guide you and the cause is almost certainly your own connection count.
- Hold AT MOST ONE connection per feed, and never more than 6 open /feed/* connections in total per egress IP — the budget is shared across /feed/chats, /feed/deaths and /feed/connections. Practically: one chats + one deaths + one connections = 3, leaving headroom. Ensure reconnect logic closes the old socket BEFORE opening the new one, or a reconnect storm will manufacture its own 429s.
- Guard against leaked sockets explicitly: a consumer that reconnects on timeout without fully closing the prior connection will climb toward the cap of 6 and then be locked out. Add a hard invariant/assertion that at most one live connection object exists per feed at any time.
- Do NOT budget rate-limit permits for the feed — it is in no resilience4j bucket, and holding a stream open costs zero permits. Budget the 5/s `main` bucket exclusively for your REST backfill calls to /chats/window; the feed and the backfill do not contend on the limiter.
- On 429 at connect, parse nothing and retry with a long backoff (start ~30-60s). Treat a 429 with `Content-Type: text/html` as an edge connection-cap rejection (your fault, reduce connections) and a 429 with a plain-text `Too many requests` body as an app-side resilience4j rejection (only reachable via REST endpoints, slow down request rate).
- Deduplicate on the client. There is no id field and no server-side dedup, and the hourly Redis listener refresh is a plausible replay window. Key on a tuple of (time, playerUuid, chat) — or a hash of the full payload — and keep a bounded LRU/ring of recent keys (a few thousand entries, or a ~2-hour time window) to survive the hourly refresh.
- Do NOT assume ordering. Each message is fanned out on its own virtual thread, so two chats milliseconds apart can arrive inverted. Never use 'time of last received event' as a high-water mark for backfill without subtracting a safety margin, and if your sink needs chronological order, buffer briefly (e.g. 1-2s) and sort by `time` before emitting downstream.
- Never treat the stream as gap-free. Because there is no id:/Last-Event-ID replay, every disconnect — including each routine 15-minute rollover — is a potential hole. Record the wall-clock interval [last_event_seen, reconnect_established] for every disconnect and queue it as a backfill range.
- Backfill gaps via GET /chats/window with sort=ASC and startDate set, paging with pageSize=100 until a 204. Rate-limit yourself to under 5 req/s (the `main` bucket; it queues up to 10s then 429s). Expect the different field shape — `uuid` instead of `playerUuid`, and NO `component` — and normalize both sources into one internal record with component nullable.
- Delay backfill of very recent gaps. The code's own scraperTimeCutoff of now-1h indicates recent DB data may be incomplete, so a gap that just occurred should be re-queried after an hour rather than immediately, or you will persist an incomplete range and never revisit it. Keep gap ranges in a durable retry queue with a 'not before' timestamp.
- Do not re-issue an identical /chats/window query expecting fresher rows — it is @Cacheable for 5 minutes and will return the same page. Vary the window (advance startDate) or wait out the TTL.
- Do not build any polling of recent data as a primary or fallback strategy. The maintainer has pre-written a guard that returns 400 'Migrate your scraping to /feed/chats' for window queries newer than an hour and merely left it commented out; it can be enabled at any time and would break a polling consumer instantly. The feed is the sanctioned live path.
- Parse `time` with a nanosecond-tolerant, variable-width ISO-8601 parser. Java: OffsetDateTime.parse / Instant.parse work directly. Python: use a parser that accepts 9 fractional digits (or truncate to 6 before datetime.fromisoformat on <3.11). JS: do not pass it to new Date() and expect fidelity — sub-millisecond precision is lost, which matters if you use time as part of a dedup key. Never slice the string at a fixed offset; the fractional digit count can vary.
- Decode `component` with a SECOND JSON parse — it is a JSON string containing JSON, not a nested object. Wrap that parse in a try/catch and fall back to the plain `chat` field, since component is the field most likely to change shape across Minecraft version bumps.
- Use the modern snake_case component schema when walking the component tree: `click_event` / `hover_event` (with click_event.action/.command and hover_event.action/.value), NOT camelCase clickEvent/hoverEvent. Legacy-format parsers will silently miss these branches.
- Prefer the flat `chat` field for text content and treat `component` as optional enrichment. `chat` is the bare message body with no `<name>` prefix and no formatting, so reconstruct display lines yourself from playerName + chat rather than relying on component rendering.
- Persist each event with your own ingest timestamp alongside the server-supplied `time`. Because `time` is generated by the upstream bot publisher (nanosecond precision, capture-time) and ordering is not guaranteed, having a local monotonic receive order is essential for debugging gaps and inversions later.
- Set the request header `Accept: text/event-stream` and disable any client-side response buffering/gzip-decompression buffering. The server already sends X-Accel-Buffering: no, but an HTTP client that buffers until a size threshold will delay or hide heartbeats and defeat the watchdog.
- If a legitimate need for more than 6 concurrent streams arises, do not work around it with multiple IPs — request access via the support Discord linked in the API docs (https://discord.gg/nJZrSaRKtb). The app has no key handling, so any accommodation is an edge-side change made by the maintainer.
- Fan out internally, not externally: subscribe once to /feed/chats per host and distribute events to all local consumers over an internal bus. Every extra HTTP connection multiplies server-side per-message send work (the server loops over every emitter for every message) and eats the 6-slot budget.
