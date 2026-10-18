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

## In-game vehicle names

The server mod prefixes vehicle and implement display names with the durable
SiN code, for example `[PJT7H] AGRIFAC Condor Endurance II`. The code stays the
same when ownership changes. Store catalog names and saved vehicle identities
are not renamed.

The Agent projects the authoritative codes to `vehicle-codes.xml` in the
server mailbox. The server resolves each saved native ID through `VehicleSystem`
and sends the code with that vehicle's network object ID. Client-generated
native unique IDs cannot be used to match the server's savegame IDs. Clients
request the projection every five seconds until it contains network mappings,
then refresh every thirty seconds; broadcasts also update changed code lists.
The client keeps the mapping if it arrives before the vehicle object finishes
loading. Both the server and connecting clients need the same server-mod ZIP.

Name hooks cover the registered type functions and live objects in
`g_currentMission.vehicleSystem.vehicles`, including both `getName` and
`getFullName`. Client log entries report the received network mapping count
and `display coverage vehicles=... coded=... prefixed=... sample=...`. Those
counts distinguish missing server codes from a name hook that has not applied.

## Live farm-to-farm transfers

`/vehicle_transfer destination_server destination_farm vehicle_code` is
initiated by a confirmed manager of the source farm and queues the operation
immediately; the destination farm does not approve it. The vehicle code
identifies its source server, save, world, and native vehicle, so the member
does not have to repeat the source farm ID. `destination_server` is included
for the multi-server workflow, but today it must be the same server identified
by the vehicle code. Selecting another server is rejected: cross-server
movement is not implemented. The destination farm is selected on that same
active FS25 world using a name-filtered farm autocomplete. The label uses the
canonical owner/farm pattern where available, such as `Repton | Repton Does`,
with an observed farm-name fallback; the command submits the corresponding ID.
Staff `/vehicle_transfer_admin` uses the same farm picker. Dispatch is allowed
only when the latest game snapshot is complete and no older than two minutes.
A staff-only `/transfer_dispatch` remains available for retry after a stale
snapshot or temporary dispatch problem.

Staff can use
`/vehicle_transfer_admin destination_server destination_farm vehicle_code reason`
for a directly authorized correction or relocation when the source-farm
manager cannot initiate it. The destination fields match the member command;
the staff command adds a required audit reason. The source server and farm are
derived from the vehicle code and live inventory. The destination server must
currently match that source server. It still applies the same fresh-snapshot,
live ownership, safety, and native readback checks. Staff authority does not
permit guessing an asset or bypassing FS25 validation.

The server resolves the exact native ID again and checks current ownership,
owned-property state, occupancy, AI control, and attachment state. It selects
an available slot from the map's native shop spawn locations, relocates the
vehicle there, and then changes its owner farm. The code stays attached to the
same server/save/world/native ID. Central marks the transfer complete only
after the mailbox receipt identifies the same transfer and asset and confirms
both destination ownership and shop-position readback. Unclear outcomes remain
in reconciliation rather than being retried as a second mutation. The
initiating Discord user's private response in the invoking channel is edited
after FS25 reports a result, while its 14-minute in-memory interaction tracking
remains available. No transfer-result DM is sent. After expiry or a bot restart,
staff can inspect `/transfer_list`; uncertain results are labeled as needing
review. If the source manager
chose the wrong destination and the transfer completed, the destination farm
must initiate a new transfer back. This first transfer path is limited to two
farms in the same active FS25 world; it does not move vehicles between servers
or saves.

## Virtual garage

An admin-only garage implementation is prepared for live validation. It uses
native vehicle XML, an independent immutable Central copy, a custody lock and
both live and saved-game confirmation. It preserves equipment for the canonical
farmer's account instead of paying its resale value during farm closure.
See the [garage guide](vehicle-garage.md) for commands, state transitions,
same-world restrictions, recovery limitations and validation requirements.
Direct farm-to-farm transfer does not require garage storage.
