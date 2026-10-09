# Farm operations telemetry

## Event path and aggregation

The dedicated server observes native `Storage:setFillLevel` changes,
`SellingStation:sellFillType`, AI job lifecycle messages, and vehicle operating
time/position. Lua coalesces storage changes by storage object, farm, fill type,
direction, and game calendar hour during the existing one-second mailbox
window. Each movement carries its total liters and original tick count. A game
hour change always starts a new record. The normal one-second mailbox write
and retry queue remain the restart boundary; storage is never held in a new
multi-minute in-memory buffer.

The server writes `farm_operations_batch` XML into its existing durable
mailbox. The Agent parses and forwards each batch without re-bucketing it.
Central validates the whole batch, then accounts for each row in Mongo before
writing the batch-level `processed_server_events` marker. Each routine storage
row is atomically added to `farm_storage_hourly`, keyed by server, save, world,
farm, game year/period/day/hour, fill type, and direction. Source positions
and runtime node IDs are retained as source evidence but do not split an hour
bucket. Missing position is recorded as `source_key="unknown"`.

Hourly documents keep Decimal128 liters, native tick count, delivery count,
first/last game times, first/last Central receipt times, source-sequence
bounds, runtime keys, and observed runtime nodes. No source event string array
is retained. A compact per-runtime sequence bitmap inside each hourly document
is updated atomically with the liters and count. A repeated delivery therefore
cannot increment a second time, even if Central wrote the aggregate but crashed
before its batch marker. A partial batch retry replays already-accounted rows
as duplicates and finishes the remaining rows. Out-of-order arrivals use the
source row's game date and hour to select the bucket.

`game_time_ms` must be in `[0, 86400000)`, and year, period, and day must be
valid (year >= 1, period 1-12, day 1-31). Invalid or missing calendar values
are kept as individual raw reconciliation events with their Central arrival
time; they are never guessed into a game-hour bucket. Sales, finance, AI,
vehicle usage, ownership, contracts, and other authoritative events keep their
current event-specific handling.

## Storage and retention

- Hourly storage aggregates and their deduplication bitmaps expire 14 days
  after the last received movement in that game hour. A movement replay is
  supported idempotently for that same 14-day window.
- Raw storage movement rows (`storage_in`/`storage_out`), including invalid-
  game-time reconciliation rows, expire after 14 days. The partial TTL does not
  shorten retention for sales, AI, or vehicle events; those retain the existing
  35-day raw window. Routine valid-time storage history remains in 14-day hourly
  aggregates, and finance changes are not TTL'd.
- `processed_server_events` retains its existing seven-day TTL. Operation
  batches do not add one marker per batch: their rows use atomic bucket bits or
  raw-event upserts for replay protection. This removes the operation marker
  stream from the collection. Heartbeat markers keep only scope and timestamp;
  their event ID is already part of `_id`.
- Finance changes have no TTL. Contract and ownership facts retain their
  existing lifecycle and idempotency rules.
- No TTL is applied to any authoritative financial event.

TTL deletion is asynchronous MongoDB maintenance. Deployment creates indexes
and starts expiry automatically; do not manually delete production rows.
Valid-time storage movements are never persisted as individual raw documents;
they are atomically accounted for in an hourly bucket before ingestion is
acknowledged. Existing source-specific hourly buckets from the previous key
format remain deduplicated during the 14-day transition window, while new
movements consolidate by farm/fill/direction/hour. Invalid-time storage rows
are retained for reconciliation for 14 days. `/farm_report` combines those
fallback records with hourly aggregates. The partial raw TTL applies only to
storage directions, so sales, AI, and vehicle events retain their existing
35-day window. Central shortens the old 90-day hourly TTL in place with
MongoDB `collMod`; expiry is asynchronous and no records are manually deleted.

## Volume and validation

The deterministic volume test models 500,000 native production ticks
coalesced into 96 server deliveries (24 game hours x two farms x two
fill/direction combinations). Central stores 96 hourly documents instead of
500,000 event documents: about **5,208x fewer documents** and the same
500,000-tick count and exactly equal Decimal128 liters. It replays rows in
reverse order, repeats half after recreating the Central service, and retries
the complete batch without changing totals.

Production validation after deployment:

1. Confirm the Central deployment created `farm_storage_hourly` indexes and
   the operational raw TTL index.
2. Load and unload a known quantity in owned storage; compare liters and tick
   count in the hourly aggregate with native before/after storage levels.
3. Replay a captured operations mailbox batch and verify the hourly aggregate
   totals do not move.
4. Confirm `/farm_report` includes the hourly movement and reports correct
   fill type, direction, farm, and game hour.
5. Check Atlas collection and index sizes after the 14- and 35-day TTL windows;
   TTL expiry is asynchronous, so counts need not drop at the exact boundary.

Deploy Central before the server mod. The Agent wire format is unchanged and
the current Agent already forwards `farm_operations_batch`. The schema
migration adds a new collection and TTL/query indexes. No new service or
infrastructure is required.
