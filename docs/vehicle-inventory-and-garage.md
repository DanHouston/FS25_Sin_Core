# Vehicle ownership boundary

`FS25_SiN_Server` observes owned equipment directly from the live server
`VehicleSystem` and includes an all-or-nothing item list in its periodic game
snapshot. Pallets, bales, big bags, leased vehicles, and mission equipment are
not farm vehicles. Each row has the native vehicle unique ID, owning FS25 farm
ID, store XML filename, optional display/mod name, and optional native sell-back
value. If an ID or source is unavailable, `vehicleInventoryReady=false`; no
partial list may be treated as complete. The Agent sends the rows to Central,
which validates them before writing the world-scoped Mongo snapshot.

`/vehicles [server] [page]` shows a verified farm manager only that farm's live
vehicles from a fresh, authoritative game snapshot. It does not change the
farm's equipment or its SiN bank account. The displayed five-character code is
an SiN alias, durably registered in Mongo against the full native vehicle ID.
A global unique index prevents two assets from receiving the same code; a rare
collision lengthens the new asset's code while retaining the old one. Alias
records are never deleted or reused after a vehicle leaves inventory. Future
commands must resolve the exact code, then re-check the native ID and owner in
the live game before changing anything. Back up `vehicle_codes` with Mongo.

Farm IDs and names are not global identities. A member who manages farms on two
servers has two distinct vehicle inventories, keyed by `server_key`,
`save_key`, `world_id`, and native `farm_id`. We do not merge them because the
same player, farm name, or farm number appears on both servers. Garage assets
must keep a persistent source farm identity and require an explicitly selected
destination farm with its own verified manager authority.

## Live farm-to-farm transfers

`/transfer_vehicle destination_server destination_farm vehicle_code` is
initiated by a confirmed manager of the source farm and queues the operation
immediately; the destination farm does not approve it. The vehicle code
identifies its source server, save, world, and native vehicle, so the member
does not have to repeat the source farm ID. `destination_server` is included
for the multi-server workflow, but today it must be the same server identified
by the vehicle code. Selecting another server is rejected: cross-server
movement is not implemented. The destination farm is selected on that same
active FS25 world. Dispatch is allowed only when the latest game snapshot is
complete and no older than two minutes. A staff-only `/transfer_dispatch`
remains available for retry after a stale snapshot or temporary dispatch
problem.

Staff can use
`/admin_transfer_vehicle destination_server destination_farm vehicle_code reason`
for a directly authorized correction or relocation when the source-farm
manager cannot initiate it. The destination fields match the member command;
the staff command adds a required audit reason. The source server and farm are
derived from the vehicle code and live inventory. The destination server must
currently match that source server. It still applies the same fresh-snapshot,
live ownership, safety, and native readback checks. Staff authority does not
permit guessing an asset or bypassing FS25 validation.

The server resolves the exact native ID again and checks current ownership,
owned-property state, occupancy, AI control, and attachment state before
calling the native owner-farm setter. The vehicle stays in the world, and the
code stays attached to the same server/save/world/native ID. Central marks the
transfer complete only after the mailbox receipt identifies the same transfer
and asset and reports an authoritative destination-owner readback. Unclear
outcomes remain in reconciliation rather than being retried as a second
ownership mutation. A failed or rejected mutation leaves ownership with the
source farm. If the source manager chose the wrong destination and the transfer
completed, the destination farm must initiate a new transfer back. This first
transfer path is limited to two farms in the same active FS25 world; it does
not move vehicles between servers or saves.

## Garage status

The garage is **not yet enabled**. A snapshot is not a durable vehicle archive,
and an FS25 vehicle cannot safely be removed on the strength of a Mongo row.
Garage deposit/retrieval needs an exact native XML save, independent durability
confirmation, source removal, destination shop-space/slot checks, asynchronous
load verification, client synchronization, and crash/restart reconciliation.
GIANTS' savegame vehicle loader can sell an item automatically when slots are
full, so it must not be used as a blind garage retrieval path. Direct transfer
of a live vehicle does not require first depositing it in the garage. The
transfer handler exists, but garage work remains separate and must not be
enabled until its save/remove/load/rollback sequence has dedicated game-server
testing.
