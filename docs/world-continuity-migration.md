# One-time pre-marker world continuity migration

The migration command in `scripts/migrate_same_physical_world.py` is for the
single class of incident where a physical save was unchanged while the
save-backed SiN world marker was being repaired. It is not a general
cross-world farm-adoption mechanism.

## What it proves and writes

The command requires all of the following before it writes anything:

- the source generation exists and is historical, and the target generation is
  the active generation;
- source and target generations carry matching physical save-slot and map
  evidence (and the target snapshot agrees with that slot);
- the target game snapshot names the expected farm and independently reports
  the expected farmland owner;
- the historical farm request and its successful, source-world provision and
  farmland receipts match the operator-supplied evidence;
- the target save identity is an unambiguous approved link and a current target
  world activity/identity observation reports the expected farm.

`--apply` creates only new target-world projections: the member farm mapping,
an `awaiting_manager` continuity request, and an observed-identity projection
derived from the current target-world observation. It records an audit row in
`world_continuity_migrations`. It does not copy operations, receipts,
reservations, sessions, generations, historical requests, or old authority.
The request is deliberately not marked active and no manager or contractor
permission is granted by the migration itself. `--reconcile` invokes the
normal current-world repair path, which queues manager and source-farm to
SiN-Harvest contractor operations subject to the existing receipt and
authoritative read-back gates.

The deterministic migration ID and audit row make retries idempotent. Any
conflicting current evidence fails closed.

## Procedure for the Hobo pre-marker transition

Run from the Central repository/service environment with the normal
`MONGODB_URI`, `MONGODB_DATABASE`, and approved operator credentials loaded.
The first command is read-only:

```powershell
python scripts/migrate_same_physical_world.py `
  --server-key sin-fs25-01 `
  --save-key sin-fs25-hobo-v1 `
  --source-world-id sin-world-20260923212757-1790213277-350359 `
  --target-world-id sin-world-20260923222548-1790216748-646517 `
  --farm-id 2 `
  --farm-name "Repton Does" `
  --farmland-id 44 `
  --discord-id 694350852915331113 `
  --unique-user-id "hiIe5rJnzMox9Jo5rggcW9cvBnbcF59wSJSSgciDzm8="
```

Proceed only if it prints `"mode": "dry-run"` and `"status": "validated"`
with the expected source/target IDs and current observation. Then apply and
start normal reconciliation in one auditable command (replace the operator
ID with the approving staff member's Discord ID):

```powershell
python scripts/migrate_same_physical_world.py `
  --server-key sin-fs25-01 `
  --save-key sin-fs25-hobo-v1 `
  --source-world-id sin-world-20260923212757-1790213277-350359 `
  --target-world-id sin-world-20260923222548-1790216748-646517 `
  --farm-id 2 `
  --farm-name "Repton Does" `
  --farmland-id 44 `
  --discord-id 694350852915331113 `
  --unique-user-id "hiIe5rJnzMox9Jo5rggcW9cvBnbcF59wSJSSgciDzm8=" `
  --operator-id <STAFF_DISCORD_ID> `
  --apply --reconcile
```

The output must report `applied` (or `already_applied` on a retry) and
current-world reconciliation operations. Agent polling then delivers the
manager and contractor operations to FS25; only successful scoped receipts and
read-back can activate them.

## Verification

Verify the current target generation has exactly one member mapping for Farm 2,
one continuity request for the approved identity in `awaiting_manager` or
`active`, and current-world manager/contractor permission jobs. Verify those
jobs and all later receipts carry the target world ID. Verify g21 remains
historical and its old operations/receipts are unchanged; none should be
copied into g22.

After `FS25_SiN_Server_world.xml` has been durably written, save normally and
restart the same physical save. Compare the marker's `worldId` before and after
restart: it must remain
`sin-world-20260923222548-1790216748-646517` while the runtime generation and
session may change. A genuinely replaced save with no marker must still create
a new world ID; the migration is not expected during normal future restarts.
