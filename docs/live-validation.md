# SiN consolidated live validation

This document lists only checks that require a real FS25 server/client. The
central services, Agent mailbox transport, persistence, idempotency, and
authorization rules are covered by the local test suite.

## One deployment/restart group

Deploy the same `FS25_SiN_Server.zip` to the dedicated server and client. Keep
the Agent pointed at the canonical `FS25_SiN_Server` mailbox. Restart the
dedicated server and reconnect the client once. Use the read-only commands
`sinSelfTest` and `sinPermissions` during the session.

### Farm Manager synchronization

1. Log directly into the personal farm and run `sinPermissions`; record the
   client-side `manager=true` and permission booleans.
2. Switch from the personal farm to `SiN Harvest`; run `sinPermissions` and
   confirm manager authority is absent while ordinary shared permissions are
   retained.
3. Switch back without reconnecting; run `sinPermissions` and confirm the
   client reports manager authority and the complete manager permission set.
4. Intentionally demote the player in-game, wait longer than the periodic
   reconciliation interval, and run `sinPermissions` again. Confirm the
   persisted SiN authority restores the client state.
5. Run `sinSelfTest` and retain the output with the server log. The self-test
   is read-only and does not repair state.

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
hourly compensation. Event creation requires a timezone-bearing ISO-8601 value
such as `2026-09-15T20:00-04:00`; Discord list/view output uses localized
timestamp markup.
