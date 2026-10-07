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
vehicles from a fresh, authoritative game snapshot. It is read-only and does
not change the farm's equipment or its SiN bank account.

Farm IDs and names are not global identities. A member who manages farms on two
servers has two distinct vehicle inventories, keyed by `server_key`,
`save_key`, `world_id`, and native `farm_id`. We do not merge them because the
same player, farm name, or farm number appears on both servers. Future garage
assets must keep a persistent source farm identity and require an explicitly
selected destination farm with its own verified manager authority.

The garage is **not yet enabled**. A snapshot is not a durable vehicle archive,
and an FS25 vehicle cannot safely be removed on the strength of a Mongo row.
Garage deposit/retrieval needs an exact native XML save, independent durability
confirmation, source removal, destination shop-space/slot checks, asynchronous
load verification, client synchronization, and crash/restart reconciliation.
GIANTS' savegame vehicle loader can sell an item automatically when slots are
full, so it must not be used as a blind garage retrieval path. Direct transfer
of a live vehicle will not require first depositing it in the garage; it will
have its own explicit owner/recipient authorization and game receipt.
