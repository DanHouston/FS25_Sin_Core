# Personal virtual vehicle garage

The first garage implementation is **admin only** and is prepared for live
validation. It preserves a native vehicle archive for a farmer's SiN account
instead of selling that vehicle during farm decommission. No farm shutdown or
cash settlement is performed by garage commands.

## Commands

All three commands require the configured Network Admin role and `#staff`.
They declare Administrator as their default Discord command permission.

| Command | Use |
| --- | --- |
| `/vehicle_garage_store_admin server:<server> vehicle_code:<code> reason:<text>` | Queue storage of one live owned vehicle. The source farm and its canonical account owner are resolved automatically. |
| `/vehicle_garage_admin member:<Discord user> [page:1]` | Inspect that account's retained vehicles and pending/review operations. Failed stores and completed retrievals are excluded. Ten rows per page, plus an attached CSV containing the full current list. Choose any existing page, starting at 1. |
| `/vehicle_garage_retrieve_admin destination_server:<server> destination_farm:<farm picker> vehicle_code:<code> reason:<text>` | Restore a stored vehicle to an owned farm on the original server/world, at the shop. The destination farm must belong to the same account holder. |

Reasons are required, 1-200 characters. Codes retain their permanent SiN
identity. Source farm numbers are not personal garage owners, and storing a
vehicle as an administrator does not put it in the administrator's garage.
Destination farms use the existing name autocomplete.

Every valid garage list response attaches `vehicle-garage-<Discord user ID>.csv`
once, including when the garage is empty. The CSV contains all current records
for that account across servers/worlds, using the same snapshot as the selected
page, with no 100-record cap. It includes vehicle codes, names, states,
server/save/world, farm IDs, UTC timestamps, reasons, receipts and vehicle
identifiers. Failed stores and completed retrievals remain excluded. The report
and attachment are private and read-only. CSV uses UTF-8 with a BOM for spreadsheet
compatibility and escapes text that could be interpreted as a spreadsheet formula.

Examples (select the farm/user with Discord's controls):

```text
/vehicle_garage_store_admin server:sin-fs25-01 vehicle_code:PJT7H reason:Retain equipment before farm closure
/vehicle_garage_admin member:@Matt70
/vehicle_garage_retrieve_admin destination_server:sin-fs25-01 destination_farm:<pick Matt's new farm> vehicle_code:PJT7H reason:Farmer returning
```

## Decommission workflow

1. Inspect the farm and choose the vehicles the farmer will retain.
2. Detach each vehicle/implement, stop AI and the engine, park it and leave it.
   Unload livestock into a barn and unload separately mounted objects/bales.
3. Store each selected vehicle by code. An export or queued response is not
   completed storage.
4. Keep FS25 and the Agent running. Allow the normal native game save/autosave
   to finish after removal. The garage waits for saved-game absence as well as
   live absence; it does not force a game save or edit a live save file.
5. Inspect the farmer's garage. Every retained vehicle must say **stored**.
   `awaiting_store_save` means the native save/checkpoint is still pending.
6. Rerun `/farm_assess_admin`. Stored personal vehicles appear as retained
   garage assets with **no sale credit**. Pending/review garage custody prevents
   a final closing figure. Inspect all other assessment coverage gaps too.
7. Proceed with any separately authorized future settlement/decommission flow
   only after all garage operations are settled. Garage commands do not implement
   farm deletion.

Native vehicle XML preserves its saved configurations, condition, operating
time and specialization state, including supported internal fill units. Those
contents are not separately paid as building stock. This is not a snapshot-row
reconstruction or a catalog replacement. Required vehicle mods/store items must
remain installed; bundles and externally mounted objects are not supported by
this first path. Behavior of custom specializations still needs game validation.

### Failed storage attempts

`failed` records are separate attempts, not additional stored copies. A
definitively failed store retains the live source vehicle and releases that
attempt's custody lock. They are excluded from `/vehicle_garage_admin`; failure
and success notices update the original store/retrieve command's private reply
while that interaction remains available. The records remain in the database
for audit. Correct the reported condition before a new request. Pending states
and `review_required` remain visible because their custody is unresolved.

`detach all attached vehicles first` rejects an attached combination. A
`vehicle changed after archiving` rejection means the second native save-state
capture differed from the independently archived copy; removal was blocked.
Keep it detached, parked, empty of occupants, with engine and AI stopped.

Server mod **0.2.2.27** names the changed XML field in the receipt and logs
`[SiN Vehicle Garage] state changed` with archived/current values. It preserves
the comparison checks; an unexplained mismatch still requires investigation.
Failure notices and garage review notes remove Lua source-location prefixes
before limiting their length. Long pages use multiple private channel replies
so the last review reasons are not silently cut off.

On the Windows game host, inspect mismatch details from the server's game log
(substitute its path if different):

```powershell
Select-String -LiteralPath 'C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\log.txt' -Pattern '\[SiN Vehicle Garage\] state changed' | Select-Object -Last 10
```

## Storage protocol and durability

1. Central atomically claims vehicle custody, records the canonical farmer,
   creates the garage item and queues `garage_prepare`. A pending transfer of
   the same native vehicle blocks storage.
2. The native server validates the live owned object and exports one vehicle
   through `VehicleSystem:saveVehicleToXML` using `Vehicle.xmlSchemaSavegame`.
   It also saves a local journal of standard XML writer output. Nothing is
   removed at this stage.
3. The Agent uploads the bounded native XML with SHA-256. Central validates the
   native vehicle ID and source farm, stores the immutable archive in MongoDB,
   and creates `garage_store` in the same transaction.
4. The Agent downloads that independent copy, checks its hash, flushes it to
   disk, reads it back and writes the local verified marker before delivering
   the deletion command.
5. The server rechecks safety/ownership and reserializes the vehicle. A changed
   save state is rejected before removal. World component position/rotation is
   excluded from comparison because parked physics can jitter and retrieval
   deliberately replaces world pose with shop delivery.
6. A durable removal-intent journal precedes native deletion. The runtime waits
   for FS25's deferred removal and then records live absence. Central moves to
   `awaiting_store_save`, preventing command redispatch.
7. After a later native save, the Agent reads `vehicles.xml` and the save-backed
   world marker. It checks stable file timestamps, a save newer than the journal,
   the exact world and vehicle absence. Central independently requires a fresh,
   complete live snapshot without the vehicle. Only then is custody **stored**.

Both the immutable Central archive and the server's native XML/journal are
retained. There is no archive TTL and no automatic sale/refund. Back up the
garage collections with the rest of MongoDB and retain the server mailbox.

## Retrieval and duplicate prevention

Retrieval requires `stored`, a fresh complete snapshot with no live copy, an
active source world, and a destination farm whose canonical account owner
matches the garage. The command atomically leases that garage item for one
retrieval. The native loader independently checks the vehicle store item,
bundle restriction, available vehicle slots, native ID absence and shop space.

The implementation uses `VehicleLoadingData` directly after its own checks.
It does not call `VehicleSystem:loadVehicleFromXML`, whose native slot-failure
path can sell a vehicle. A callback must confirm one restored object with the
original native unique ID, destination owner and shop position. Native loading
uses the archive's saved state with an explicit shop position and destination.

The archive stays held while `awaiting_retrieve_save`. Only proof of both live
and saved-game presence under the destination owner marks it **retrieved** and
releases custody for later ordinary transfer or another garage cycle. Historical
archive bytes remain for audit; they cannot authorize another retrieval.

An occupied/full shop, missing mod or failed preflight leaves the archive
stored when the game proves no mutation began. Correct the cause and issue a
new retrieval request. A crash, partial load, conflicting receipt or unproven
removal produces `review_required`; it does not authorize a blind second load.
A removal-intent journal alone is not proof of completed removal. Recovery
from an uncertain operation requires operator reconciliation; there is no
force-success or replay-unknown-outcome command in this first release.

First-version retrieval is limited to the **original active server/save/world**.
Personal garage records survive farm/world decommission, but replacement-world
or cross-server imports need a separate migration/compatibility workflow.

## Discord acknowledgement

Final storage/retrieval and failure notices use the existing private command
response, not a DM. The in-memory interaction tracking lasts 14 minutes and
does not survive a bot restart. The garage list is the durable status view.
Completion notices are written transactionally with custody completion.

## Authenticated transport

The new HTTP routes are Agent routes, protected by server credentials, configured
save mapping and the active world. They do not expose admin command creation.

| Route | Purpose |
| --- | --- |
| `GET /api/server/garage/archive` | Download an existing archive for an active authorized garage operation. |
| `GET /api/server/garage/pending` | List this world's operations awaiting native save proof. |
| `POST /api/server/garage/capture` | Supply native XML for an already queued preparation; maximum 2 MB decoded XML. |
| `POST /api/server/garage/checkpoint` | Submit saved-game presence/absence evidence; Central also checks its live snapshot. |

Ordinary operation delivery and receipts use the existing authenticated routes.
The Agent's checkpoint reader is confined to `savegameN` directories directly
under its FS25 game profile. A different custom save-directory layout cannot
produce checkpoint proof in this first implementation.

## Deployment and validation status

The initial garage requires Central/API, JiN, Agent and **FS25_SiN_Server 0.2.2.26**
or newer. The field-mismatch diagnostics require **0.2.2.27** and the updated
Central/JiN display formatter; no additional Agent change is required. Server
and clients need the identical mod ZIP. Updated ZIPs are directly in `dist`.
Database initialization adds indexes for the new collections. No live data has
been moved by preparing the source, and nothing is deployed automatically.

Behavioral and in-game validation are pending. Before using this for the actual
departing farmer, validate a disposable owned vehicle through archive, removal,
native save, retrieval at the shop and a second native save. Compare native
IDs, configurations, condition, operating time and fills. Include full-shop,
missing-mod, duplicate requests, interrupted capture/removal/loading, server/bot/
Agent restart, and save-before/after restart cases. Check that live plus garage
custody never produces a second usable copy or sale credit, that account
ownership survives farm decommission, and that only staff can invoke the commands.

Native reference: [FS25 VehicleSystem](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=91&class=897&version=engine)
and [VehicleLoadingData](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=91&class=893&version=engine).
The installed SDK's `Vehicle:delete` confirms that native deletion is deferred;
the runtime therefore waits for live absence rather than assuming synchronous deletion.
