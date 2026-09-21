# Authoritative farmland ownership assignment

This workflow assigns one FS25 **farmland ID** to one FS25 farm in one
server/save context. It does not use field geometry, does not infer parcel
boundaries, and does not fabricate farmland polygons. Courtright Line has 72
farmland IDs and fewer field geometries; a field is not a farmland parcel.

## Lifecycle and authority

`/farm_approve` now only creates or adopts the approved farm. Its successful
FS25 receipt moves the request to `land_pending`; it grants no manager role.
An operator then makes one explicit decision with:

```text
/farmland_assign server:<server> member:<land-pending requester> farmland_id:<ID>
```

The command is limited to the configured staff/operator boundary, resolves the
requester's stable identity and persisted FS25 farm ID server-side, and records
the operator ID. It does not accept a farm name or client-provided authority as
a security identity. Central requires one known server/save request, a positive
farmland ID present in the current authenticated snapshot, an existing target
farm ID in that same snapshot, and a currently unowned preflight owner (`0`).
An occupied parcel is denied rather than reassigned. A request already in
`land_assigning` only returns the same operation ID for the same farmland;
another target is denied.

The snapshot is a preflight guard, not mutation proof. The command XML carries
the opaque operation ID, Central server/save scope, farmland ID, target farm ID,
request ID, and the recorded authorizer. The Agent merely persists/delivers this
mailbox command and receipt; it has no Mongo dependency.

## FS25 authoritative boundary

`FS25_SiN_Server:processLandCommand` first checks that the command server key
matches the running server, resolves the farm with
`g_farmManager:getFarmById(farmId)`, and resolves the farmland with the runtime
methods used by the installed mod:

```lua
g_farmlandManager:getIsValidFarmlandId(farmlandId)
g_farmlandManager:getFarmlandById(farmlandId)
g_farmlandManager:getFarmlandOwner(farmlandId)
g_farmlandManager:setLandOwnership(farmlandId, farmId)
```

For an unowned parcel it calls `setLandOwnership(farmlandId, farmId)` inside a
protected call and then obtains `getFarmlandOwner(farmlandId)` again. The
post-call owner must equal the requested farm ID before the receipt is
successful. The Lua return value of `setLandOwnership` is not proof. A parcel
already owned by the target performs no mutation and returns
`already_satisfied` only after a second owner read. A foreign owner, unknown
farmland, or unknown farm returns `rejected`; a call/readback failure returns
`failed`.

The mod writes a durable `networkLocalReceipt` containing operation and scope,
farmland and requested farm, `owner_before_farm_id`, observed
`owner_farm_id`, `mutation_performed`, status, and reason. Best-effort
`FarmlandStateEvent` broadcasting is visual replication only and cannot change
the ownership outcome. Central accepts `applied` or `already_satisfied` only
when IDs, scope, and observed owner exactly match the persisted operation. It
then succeeds the operation once and begins the existing manager-permission
handoff. Bad/mismatched evidence returns the request to `land_pending` or marks
the operation `reconciliation_required`; it never advances authority.

The listed Lua method shape is the current mod-facing integration path and is
covered by deterministic transport/reference tests. Its exact behavior against
the deployed FS25 build, including the client replication event shape, remains
a required live validation gate.

## Idempotency and recovery

Commands remain on disk until the mod creates a receipt, and receipts remain on
disk until Central accepts them. Agent/Central interruptions therefore retry the
same operation. Re-delivering the same command finds the current owner:
already-target is a non-mutating success; a foreign owner is denied. Central
does not create a second state transition for a successful duplicate receipt.
Runtime generation changes do not invalidate a save-scoped land command by
themselves: owner read-back makes replay after an FS25 restart safe.

`/farmland_status server:<server> farmland_id:<ID>` gives staff the latest
authenticated snapshot owner before an assignment. In the dedicated server
console, `sinFarmland <ID>` reads `g_farmlandManager` directly and logs the
current owner for live evidence.

## Required real-FS25 validation

Automated scenarios use a deliberately separate mutable reference owner map;
they do not emulate or prove GIANTS APIs. On a disposable or explicitly safe
live save:

1. Run `sinFarmland X` for a known unowned farmland and retain its owner.
2. Use `/farmland_assign` for a `land_pending` farm and record the operation ID.
3. Verify the FS25 farmland UI and `sinFarmland X` both show the destination
   farm ID.
4. Inspect the operation receipt/status through the Discord response and
   `/farmland_status`; confirm pre/post owner evidence agrees.
5. Repeat the exact operation/idempotency path; verify `already_satisfied` and
   no second mutation.
6. Save/restart FS25, run `sinFarmland X`, and confirm persisted ownership.

Do not call this workflow LIVE PASS until all six checks are retained with the
exact release artifact/hash. Farmland parcel geometry remains unresolved and is
not needed for this ID-based ownership mutation.
