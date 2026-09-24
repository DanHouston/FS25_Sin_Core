# SiN consolidated live validation

This document lists only checks that require a real FS25 server/client. The
central services, Agent mailbox transport, persistence, idempotency, and
authorization rules are covered by the local test suite.

## v0.1.24 live-validation checkpoint

The released baseline is `v0.1.24` at commit
`c9bcb980dd50e951a80cf076003f6ad033e3e855`. Real FS25/Mongo/Discord evidence
validated deployment, clean startup, Courtright Line map acquisition, stable
identity across reconnect, personal-farm manager restoration, active/idle/AFK
accounting, normal disconnect, durable Agent and Central retry, runtime
generation rollover, and durable JiN contract interactions.

The same evidence found five runtime/domain issues and two operational issues:
farm observation lagged the authoritative farm, SiN Harvest contractor access
was absent, one historical session remained active, the PDA map was vertically
inverted, work contracts exposed an invalid network-wide scope, JiN dynamic
autocomplete could degrade while commands remained registered, and a direct
Agent restart lacked launcher configuration. Courtright Line also confirmed
that farmland IDs and field geometry are different layers: ownership may use
authoritative farmland IDs, but no farmland polygon is fabricated from field
polygons.

The following remain explicitly synthetic-only or live-deferred: positive
cross-farm acceptance, forced renderer failure, fresh identity registration,
second-server isolation, actual farmland parcel geometry, and the vanilla
cause of reconnecting at the shop. The negative self-farm acceptance path was
live-proven; it must not be generalized into a live positive-acceptance claim.

The automated suite also covers disconnect watermark ordering, durable receipt
reconciliation, contract scope selection, and runtime map persistence/rendering.
It does not replace live validation of GIANTS runtime geometry extraction,
registration warnings, command execution, Discord permissions, or attachment
delivery.

## Farmland ownership assignment — LIVE REQUIRED

The current implementation adds a deterministic command/receipt scenario for
explicit farmland ownership, but it is not live-proven. Field and farmland stay
separate: use the authoritative farmland ID only, and do not infer a parcel
polygon from a field. On an exact deployed artifact, choose a safe unowned
farmland, request it through `/farm_request`, record `sinFarmland <ID>`, then
approve that request with `/farm_approve`. Retain the FS25 UI result, direct post-mutation
`sinFarmland` output, operation ID/receipt pre/post owner values, and Central's
single reconciliation result. Repeat the idempotent path, save/restart FS25,
and verify the same owner remains. See
[farmland-ownership.md](farmland-ownership.md) for the complete gate.

The machine-readable checklist is [live-validation-manifest.json](live-validation-manifest.json).
It names the six authoritative scenarios that must pass before collecting the
corresponding live evidence: `registration`, `control_plane_backlog`,
`activity_disconnect`, `map_contract`, `contract_scope`, and
`authority_regression`. The manifest also records the semantic and lower-level boundary
scenarios used to implement them. Release builds copy both that manifest and the
secret-free `integration-campaign.json` into the release directory.

## Farm economy capability gate

The source-backed boundary for farm cash, loans, parcel pricing, and farm
deletion is recorded in [farm-economy-capability-audit.md](farm-economy-capability-audit.md).
Do not enable starting cash/loans, FS25 money administration, cash-to-bank
movement, or farm deletion until its dedicated-save runtime probes establish
the exact FS25 APIs, replication, persistence, and read-after-write evidence.
The roster’s Farm 14 anomaly is a captured runtime-farm record with an empty
name; it remains an investigation item, not an authorization to mutate it.

New mod runtimes put the final emitted minute sequence on disconnect. Central
returns a retryable response until all minutes through that watermark are
committed, then emits one durable summary containing duration, active, idle,
and AFK totals. Legacy disconnects without a watermark retain the compatible
policy of closing from minutes already committed; they should be upgraded when
ordering guarantees are required.

## FS25 world-generation boundary — Hobo's Hollow live gate

`server_key` and `save_key` are endpoint selectors, not an FS25 world
identity. The server mod persists one opaque marker through GIANTS'
`FSCareerMissionInfo.saveToXMLFile` hook in that save's
`careerSavegame.xml`. It reads the marker unchanged on normal restart; a
replacement/fresh save without the marker receives a new marker. A legacy
`FS25_SiN_Server_world.xml` sidecar is read once for migration, but is not the
authoritative persistence location because FS25 may rewrite the save directory.
This is a SiN-managed marker, not a claimed GIANTS save UUID. The career-save
hook and restart behavior remain **LIVE VALIDATION REQUIRED**.

Fresh saves can expose their save directory only after FS25 completes its first
successful save. A new or legacy-migrated marker also remains unavailable for
world-scoped traffic until that career-save hook has written it, so a crash
before the first save cannot publish an identity that would be regenerated on
restart. The server mod therefore keeps world-scoped traffic fail-closed and
retries marker initialization during the same runtime when the save directory
or save-hook API was only temporarily unavailable. A readable marker with no
world ID remains a hard failure and is never silently replaced.

Until a snapshot bearing that marker reaches Central, every world-bound action
is fail-closed. Central archives legacy/no-marker rows and prior marker rows;
they remain audit history but cannot be served as current farms, farmland,
permissions, operations, receipts, or map state. Network identity and the
central ledger are deliberately not deleted. FS25 numeric IDs are therefore
only meaningful as `(server_key, save_key, world_id, numeric_id)`.

Use the existing Hobo's Hollow replacement as the first non-mutating proof:

1. Deploy the artifact and restart FS25. Retain the Agent-visible
   `snapshot.xml` `worldId` and the server service log line showing the save
   marker was read or created. This procedure does not require a dedicated
   server console.
2. Confirm the Agent snapshot marker is accepted by Central. Central's `world_generations` row for
   `{server_key:"sin-fs25-01", save_key:"sin-fs25-main", state:"active"}`
   must contain the exact `snapshot.xml` marker. (This is an operator evidence query, not a request
   to hand-edit Mongo.)
3. Run `/farm_roster` and `/farmland_status 22`. With the stated empty Hobo's
   Hollow precondition, neither historical SiN Harvest/Repton Does nor the
   Courtright Farm 2/Farmland 22 relationship may appear.
4. Retain Central/Agent logs for any old pending operation. It must be marked
   `world_superseded` or rejected because its command/receipt marker differs;
   do not reissue it.
5. Only after those proofs, validate fresh onboarding. The game must create or
   adopt the requested farm, assign the requested farmland with owner readback,
   and then issue personal manager authority. Establish a new SiN Harvest map
   explicitly; no old Farm 1 mapping may be adopted.

Do not test starting cash, loans, player positioning, deletion/reset, money
administration, deposits, or withdrawals on Hobo's Hollow. Their GIANTS
capability status remains live-deferred. In particular, both bank bridge
directions require the explicit `fs25_money_bridge_enabled` capability after
receipt-gated debit/credit, read-back, replication, and restart tests prove the
target build; the old `withdrawals_enabled` flag is not sufficient.

## One deployment/restart group

Deploy the same `FS25_SiN_Server.zip` to the dedicated server and client. Keep
the Agent pointed at the canonical `FS25_SiN_Server` mailbox. Restart the
dedicated server and reconnect the client once. Use the read-only commands
`sinSelfTest` and `sinPermissions` during the session.

### Farm Manager synchronization

1. Log directly into the personal farm and run `sinPermissions`; record the
   client-side `manager=true` and permission booleans.
2. Switch from the personal farm to `SiN Harvest`; run `sinPermissions` and
   confirm personal manager authority remains tied to the personal farm while
   the independent SiN Harvest contractor permission set is present.
3. Switch back without reconnecting; run `sinPermissions` and confirm the
   client reports manager authority and the complete manager permission set.
4. Intentionally demote the player in-game, wait longer than the periodic
   reconciliation interval, and run `sinPermissions` again. Confirm the
   persisted SiN authority restores the client state.
5. Run `sinSelfTest` and retain the output with the server log. The self-test
   is read-only and does not repair state.

### SiN Harvest shared contractor authority â€” LIVE REQUIRED

The v0.1.25 validation confirmed this as a real failure: a personal Farm Manager
did not receive the intended independent SiN Harvest contractor permissions.
The corrected policy is deterministically covered through Central, Agent XML,
and a reference executor, but that is not GIANTS runtime proof. On the exact
deployed artifact:

1. Connect an approved, linked member who is Farm Manager of a personal farm;
   a player in FS25 farm 0 is intentionally ineligible until they join/create
   a real source farm.
2. Confirm Central has both server/save/world-scoped memberships: personal
   `farm_manager` and SiN Harvest `contractor`, with `source_farm_id` equal to
   the member's current farm and target equal to the authoritative SiN Harvest
   farm ID.
3. Confirm the Agent XML contains `sourceFarmId` and `farmId`, then run
   `sinPermissions` after switching to the source farm. The server log and
   receipt must show native `setIsContractingFor(targetFarmId, true)` followed
   by `getIsContractingFor(targetFarmId)==true`; no target-farm membership is
   required.
4. Reconnect, restart the Agent, restart Central, and restart FS25 one at a
   time; confirm the same source-to-SiN Harvest edge is reconciled and remains
   usable. A delayed/old-world receipt must be rejected and quarantined.
5. Repeat with another approved member and confirm neither member's personal
   manager relationship changes. Remember that native FS25 contracting is
   farm-to-farm, so other members of the same source farm may inherit the edge.
6. On a disposable identity whose approved membership is removed, confirm the
   durable native revocation receipt reads `contracting_for=false` and restores
   the source farm's relationship without demoting the member's unrelated
   personal-farm manager authority. If a legacy target-only row is present,
   verify the bounded cleanup is receipt-gated; do not edit Mongo manually.

Do not call shared contractor authority LIVE PASS until those observations and
the exact release hash are retained.

### Activity telemetry

During the same connected session, move enough to produce an active minute,
remain still for at least ten complete sample minutes, and, if practical,
remain still for minute eleven to observe AFK classification. Disconnect and
reconnect. A staff operator can inspect the resulting aggregate and recent
sessions with `/activity_status`. Partial minutes and disconnected time must
not be counted.

The producer recovers tracking during the existing heartbeat reconciliation
when the initial connection hook or staged farm/registration state was missed.
On a remote client, `sinSelfTest` reports server-authoritative telemetry as
unavailable; run it on the dedicated server for the tracker count. Normal
Discord usage is `/activity_status`. Staff may use
`/activity_status member:@Player` without copying a raw FS25 identity.

### Map discovery probe

On the dedicated server console, run `sinSelfTest` once after the server has
loaded the save. Retain the `Map probe` result with the server log. It is a
read-only diagnostic: it reports map identity, terrain size, field/farmland
counts, the Field 22 relationship and available geometry metrics without
printing filesystem paths or copying proprietary assets. Do not treat the
probe as a registered central map until a normalized payload has been
validated and explicitly registered by an operator.

### Chat boundary

The central chat message/event schema and authenticated transport are locally
tested. The current generic mod deliberately does not call an undocumented
GIANTS chat-injection method. If a verified runtime adapter is added, test one
FS25-to-Discord message and one Discord-to-FS25 message, then confirm the same
message is not mirrored back. Until then, mark game-chat display as deferred;
do not treat a `pending_validation` receipt as successful delivery.

### Value-transfer boundary

Do not test deposits, withdrawals, vehicle transfers, or product transfers in
the live save until their exact GIANTS money/ownership/fill APIs have been
verified in the target game build. The central workflows intentionally require
durable receipts and keep unverified game mutations out of the success path.

## Results to retain

Capture:

- server and client ZIP SHA256 values
- `sinSelfTest` and `sinPermissions` output for the farm-switch sequence
- the `sinSelfTest` `Map probe` line and the active map/PDA visual comparison
- the corresponding server authorization/direct-connection log lines
- `/activity_status` output after disconnect/reconnect
- any chat or transfer operation IDs only if a verified runtime adapter is
  being tested

Do not include server credentials, binding XML contents, pairing codes, or
MongoDB connection strings in the evidence.

## Community workflow notes

The two chat directions are tested independently. The current build reports
Discord-to-FS25 injection as unavailable because no verified GIANTS adapter is
enabled; a queued operation must never be described as displayed. FS25-to-
Discord capture is also deferred until a source-backed runtime hook is proven.

`/balance` reports the central SiN wallet and pending operation amounts. The
game balance is explicitly unavailable until the snapshot contains a verified
farm-money field. `/deposit amount` resolves an unambiguous registered
server/save automatically. Withdrawals remain disabled unless a server
explicitly enables a verified FS25 money-delivery adapter.

`/contract_create` uses work type, comma-separated numeric fields, and fixed or
hourly compensation. Its server selector must resolve the creator's registered
identity context; one eligible context is selected automatically, while
multiple, missing, or ambiguous contexts fail closed. Work contracts are never
network-wide and persist explicit server/save scope. If `channels.jobs` is
configured, verify that a new contract card appears and that Accept remains
safe after a bot restart. Event
creation accepts a timezone-bearing ISO-8601 value such as
`2026-09-15T20:00-04:00`, or local `2026-09-15 20:00` using the configured
`DISCORD_TIMEZONE`; Discord list/view output uses localized timestamp markup.
If `channels.events` is configured, verify the Join/Leave board card and its
participant count.
