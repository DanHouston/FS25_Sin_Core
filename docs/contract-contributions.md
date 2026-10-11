# Native FS25 contract contribution telemetry

## Existing instrumentation audit

- `mods/FS25_SiN_Server/NetworkLocal.lua` bridges native contract lifecycle events from `SiN_FS25_Contracts` into authenticated, world-scoped mailbox events. `mods/SiN_FS25_Contracts/scripts/SiNContracts.lua` observes MissionManager generation/acceptance/disposition and supplies mission ID/type, field/area, reward estimate, accepting farm, and lifecycle.
- `fs25_network_core/native_contracts.py` previously maintained a current-state projection only. It now also writes an idempotent `native_contract_lifecycle_events` history row per lifecycle event, retaining receipt time separately from FS25 game runtime/calendar fields. The projection remains in `native_contracts`.
- `mods/FS25_SiN_Server/NetworkLocal.lua` activity/session telemetry records player connectivity and active/idle/AFK minutes using stable FS25 identity. That is presence data, not work evidence and is never used to assign contract work.
- `mods/FS25_SiN_Server/FarmTelemetry.lua` already observed generic `AI_JOB_STARTED`/`AI_JOB_STOPPED` with farm, job, vehicle, and duration. Those observations are not proof of contract work and are not used as contribution totals.
- Native FS25 missions and SiN community contracts are different workflows. This report covers native MissionManager field missions only. SiN community contracts in the `contracts` collection are not linked to MissionManager or this work stream.

## Added measurement path

On the dedicated server, `WorkArea.onUpdateTick` runs its native processing callback and writes `lastWorkedHectares` only when the callback returned positive real worked area. The SiN wrapper observes that result after native processing. It resolves the work-area triangle's world-space center through `MissionManager:getMissionAtWorldPosition`; it links a row only if the returned field mission is running and has a stable mission ID, field ID, and accepting farm. This is the contract association evidence.

The actor is a player only when the operated root vehicle exposes `controllerUserId` and the server resolves that ID to a stable FS25 unique user ID. It is AI only when a native AI job-start observation is active for the same vehicle ID. Otherwise actual worked area is retained as `unknown`, with a reason; no online/session/acceptor/farm-owner/progress inference is used. Operator, AI-job, vehicle, implement, work-area activity, mission, farm, and runtime changes split the aggregate key.

Work is accumulated in memory into rows no longer than 60,000 FS25 runtime milliseconds. Each row carries measured processed hectares, productive update `dt`, tick count, interval endpoints, runtime ID, and `work_area_processing` evidence. Rows use the existing checkpointed `farm_operations_batch` mailbox. The one-second local checkpoint preserves incomplete windows through normal restart; a hard process/power loss can still lose the small amount observed since the last checkpoint. The normal batch file is acknowledged/removed only after Central accepts it. Central stores each interval in `farm_operational_events`, keyed idempotently by server + save + world + source event ID. The new partial index serves mission/world report queries; existing retention bounds operation records.

Lifecycle history and work intervals are both isolated by `server_key`, `save_key`, and `world_id`. A replay into a different world is a separate identity; stale-world ingestion continues to follow existing Central scope rejection rules.

## Reporting

Staff can run `/contract_report server:<server> mission_id:<native mission ID>`. The private report shows lifecycle transitions, measured work hectares and productive hours by player/AI/unknown, and attribution coverage as known-attributed hectares divided by all measured hectares. Player IDs are resolved to approved canonical names when available; otherwise a stable ID suffix is shown. The report deliberately says it is observed work, not a reconstruction of total contract progress. Actual payout is shown only if a contract-linked native payout value exists; current hooks do not expose a reliable per-contract payout transaction, so it normally reports unavailable. The native reward offer is not mislabeled as paid amount.

## Measurement limits

- `worked_area_ha` is actual processed area reported by the implement's work-area callback. It can count rework/repeated passes and is not unique field coverage, crop yield, liters, or contract completion percentage.
- `productive_ms` is accumulated from the FS25 update `dt` on ticks where positive work area was reported. It is game-runtime measurement, not guaranteed wall-clock time under every time-scale/mod combination. Different implements can work simultaneously, so summing their productive time can exceed wall-clock elapsed time; compare with live dedicated-server observations before using it for reward economics.
- Work linkage uses the game's mission-at-position lookup for the processed work-area center. Missed/ambiguous locations are omitted rather than guessed; unusual tools that do not use the standard `WorkArea` specialization are not yet covered.
- Player work is attributable only when the server exposes the controlling user and stable user ID at that update. AI work is attributable only while a matching native AI-job observation exists. Missing/restarted state remains unknown. Parallel or unusual automation should be checked in live validation.
- We do not yet record per-contract native cash payout: unrelated farm finance events cannot safely be associated by farm and timestamp alone.
- Old missions/work performed before deployment cannot be backfilled. Native lifecycle records observed by the previous code may lack the new game-clock detail.

## Automated validation and deployment

Automated tests cover lifecycle idempotency/world separation; manual player identities, multiple players, AI handoff, unknown operator, measured-area-only capture; malformed intervals/identity; duplicate contribution delivery; report scope and attribution coverage; and checkpointed event persistence using the existing Lua test harness.

Deploy updated `FS25_SiN_Server.zip` and `SiN_FS25_Contracts.zip` to the dedicated server (both are produced under `dist/`), and deploy the updated SiN Core agent/Central/bot release for lifecycle history, interval validation/indexes, and `/contract_report`. The server mod should be present on clients as usual for multiplayer mod synchronization. No database backfill is required; the new index is created by normal Core database initialization. Restart FS25 to install the native work-area hook. Do not use the reported values for reward credits until the live validation below passes on the dedicated server.

### Dedicated-server validation procedure

1. Start a fresh test contract, note its native mission ID, field and accepting farm, and run it manually for several separated passes. Confirm `/contract_report` shows the player's stable identity and measured hectares; idle time near the field must add nothing.
2. Run a comparable contract entirely with AI. Confirm AI hectares/time appear only while a matching AI job is active; compare against the in-game field result.
3. Run a mixed contract with AI, stop AI, finish manually; then reverse the handoff. Confirm intervals split and totals do not assign AI/driver time across the handoff.
4. Have two players take turns operating on a contract-owned farm. Confirm two distinct player rows. Disconnect the operator while no work is occurring (no extra work should appear); if work continues without a resolvable controller, verify it becomes unknown.
5. Cancel a native mission after work. Confirm lifecycle says cancelled, the work intervals remain, and no payout is claimed. Confirm completed/expired lifecycle timestamps are separately recorded.
6. Replay an already accepted mailbox batch or retry its same event ID. Verify Mongo contains one row and totals do not change. Restart during a partially accumulated interval and verify checkpoint recovery sends the preserved row once, with a new interval after restart.
7. Load a different save/world generation and query the previous mission ID. The report must not return old-world contributions.
8. Compare measured area against the field's native completion/visual result and check standard, modded, attached, and mission-leased implements. Record any missed work-area types and attribution gaps before using the report for policy decisions.
