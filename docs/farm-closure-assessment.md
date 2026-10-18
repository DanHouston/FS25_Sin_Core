# Farm closure assessment

`/farm_assess_admin server:<server> farm:<owner/farm name>` is an admin-only,
read-only estimate for a farmer considering leaving. Use it in the configured
staff channel. Both selectors autocomplete; farm choices come from the chosen
server's current world and use the same owner/farm-name pattern as transfers.
The response is ephemeral in that channel and includes a CSV inventory.

The command does not queue closure, sell assets, move crops, credit a wallet,
or change farm membership. Actual settlement and shutdown are a later feature.

## Calculation

```text
Farm cash
+ owned equipment resale
+ owned buildings resale
+ owned land value
+ harvested inventory / products held in buildings
+ livestock current sale value
- native farm loan
= estimated net farm equity

Proposed new account credit = floor(max(0, net farm equity))
Projected account balance = existing SiN account balance + proposed credit
```

The existing SiN balance is displayed separately and is never included in the
new credit. Negative equity shows an uncovered debt amount; it does not debit
the recipient or authorize a farm closure. Currency calculations use Decimal
in Central and the proposed credit rounds down to whole game-currency units.

To retain equipment, use the admin [virtual garage](vehicle-garage.md) first.
Wait for **stored** after both live removal and a native save. Retained garage
vehicles are listed separately with no cash credit; pending/review custody
blocks the final proposal. They remain the farmer's personal assets after the
farm is decommissioned. This integration requires mod 0.2.2.26 and updated core/Agent.

The recipient comes from one active canonical `sin_farms` member-farm mapping
for the exact server/save/world/farm. The command never treats an online worker
or the first farm manager as the owner. Ambiguous mappings and system farms
have no proposed personal-account payout.

## Valuation and coverage

- **Equipment:** the current live vehicle's native `getSellPrice`. Leased and
  mission equipment receives no asset credit. The CSV lists those exclusions.
- **Buildings:** native `getSellPrice`; where unavailable, preserve the existing
  equity policy of 50% of the live paid price and label it as an estimate.
- **Land:** native runtime farmland ownership and parcel price, not field size.
- **Stored goods:** current positive fill levels in farm-owned building
  storages: silos, silo extensions, production storages and husbandry storages.
  Each physical storage is counted once and must belong to its building's
  farm. This includes stored inputs and supplies as well as harvested crops
  and finished products. Vehicle/implement contents, loose bales and loose
  pallets earn no stock credit. Unload goods into building storage first.
  Parking a loaded trailer or loose bale inside a shed does not register it
  as building stock. An unfamiliar registered storage with contents is flagged
  until its building association can be established.
- **Bale & Pallet Storage:** native `PlaceableObjectStorage` contents are
  eligible building stock. The mod serializes the live stored entries into a
  temporary XML handle through the native storage serializer, reads each
  object's `className`, `farmId`, `fillType` and actual `fillLevel`, and then
  discards the handle without saving a file or spawning/removing anything.
  Supported entries are native `Bale` and `Vehicle` (pallet/big-bag) records.
  Contents are summed by building and fill type, with the object count in the
  CSV notes. Partially filled bales/pallets contribute only their actual liters;
  fill type is the current material, with no assumed future fermentation value.
  Each object must belong to the building's farm. Mission bales receive zero
  credit and an exclusion row. Unknown formats, unreadable ownership/amounts,
  duplicate entries, inconsistent native counts or active unloading keep the
  estimate incomplete. Loose objects outside the native storage remain excluded.
- **Livestock:** owned barns' native `getClusters()` groups, with each group's
  `getNumAnimals()` multiplied by its current per-animal `getSellPrice()`.
  The native sale value reflects the live animal state; the report does not
  substitute purchase prices. The CSV lists animal subtype, barn, count,
  price per animal, total and age where available. Group counts must agree
  with the barn's `getNumOfAnimals()`. Missing counts or prices prevent a
  proposed credit; an empty barn with a confirmed zero count is complete.
  Loaded livestock trailers must be unloaded into an owned barn first: native
  trailer resale already includes part of the animals' value. Ridden animals
  must return to their barn. These conditions keep the preview incomplete
  instead of crediting the same animal as both equipment and livestock.
- **Stock prices:** highest readable current outside selling-station quote.
  If absent, a readable positive native economy price is a labeled fallback
  estimate. The CSV includes quantity, unit price, source location, value,
  valuation basis, and station/notes. Missing prices remain unknown.

As requested, standing crops are excluded. Forestry, ground heaps, vehicle
operating fuel, sale fees, and delivery costs are outside this estimate.
Bunker silos still require a separate inventory; their contents are flagged,
never silently valued at zero. Ordinary registered building storage and native
bale/pallet object storage are supported. Native asset
saleability, actual disposal and any closing charges remain work for the future
settlement process. This is an estimate of the listed assets, not a guaranteed
offer from a buyer or an executable settlement quote.

## Completeness

The command requires the active world and a confirmed farm in a game snapshot.
It checks both Central receipt time and the source XML's file modification time
carried by the Agent; both must be within two minutes (allowing 30 seconds of
forward clock skew). Repeatedly uploading an old XML cannot freshen the asset
inventory for this report.

Unreadable cash/loan, missing categories, unidentified ownership, duplicate
assets, missing prices, unresolved recipient mappings, and relevant pending
cash/asset operations are listed. Native contract liabilities use the fresh
in-game mission list, including preparing/running jobs and finished missions
awaiting collection. A complete empty list confirms zero current liabilities.
Stale accepted lifecycle events do not override that list; they remain labeled
historical observations in the CSV, with no history deletion or invented
completion/cancellation event. Missing or invalid live inventory keeps the
estimate incomplete instead of treating it as zero contracts. This check uses
the same two-minute source/receipt freshness requirements as the asset inventory.
A report with gaps shows known subtotals and
an unavailable proposed credit. Missing values are not substituted with zero.
Assessment schema 2 explicitly declares `building_storage_only` stock scope.
Older snapshots can show legacy known resale/land subtotals while awaiting the
updated mod and Agent, but cannot produce a payout figure under the new policy.

## Components and deployment

- **JiN/core source:** adds the command, calculation and CSV renderer. Restart
  JiN from the updated release so its normal command sync registers the command.
- **Agent:** parses the assessment's complete live contract list, validates
  count/identity/state and carries it with the existing source timestamp in the
  authenticated snapshot. An older mod or malformed list is marked unavailable.
- **FS25_SiN_Server 0.2.2.28:** includes native stored bale/pallet quantities
  and the live mission list alongside building stock and livestock. Install the
  identical ZIP from `dist/FS25_SiN_Server.zip` on server and clients and reload
  FS25. Other gameplay mods do not need changing for this command.

The existing Central snapshot endpoint persists the optional observations;
there is no new API endpoint, collection or database migration. Inventory is
bounded to 10,000 rows per farm and 50,000 parsed rows per snapshot. An optional
inventory exceeding the snapshot byte budget is marked unavailable, while
ordinary snapshot transport continues. Parser faults do not discard the
unrelated cash/identity observations.

The itemized observations live under `server_snapshots.farm_assessments` with
`farm_assessment_ready` and `farm_assessment_observed_at`. The command performs
database reads only and creates the CSV in memory. It does not retain a signed
or locked quote; rerun it after assets, prices or debts change.
Per-farm records now include `native_contract_inventory_ready` and
`native_contracts`. Stored object rows use `storage:object:<building unique ID>:<fill type>`
identities within the existing building-only stock scope. No database migration
or lifecycle-event rewrite is needed.

## Validation status

The operator's initial reports confirmed building/equipment/land observations
but exposed loose-item enumeration as unavailable and livestock as unvalued.
The revised building-only policy removes loose-item enumeration entirely.
Native livestock getters and trailer resale behavior were checked against the
installed FS25 SDK's `PlaceableHusbandryAnimals.lua` and `LivestockTrailer.lua`.
The stored-object serializer and mission list/getters were checked against the
installed SDK's `PlaceableObjectStorage.lua`, `MissionManager.lua` and
`AbstractMission.lua`; stored Bale/Vehicle fields were also confirmed from the
operator's FS25 save. The native object serializer is documented in
[GIANTS PlaceableObjectStorage](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=78&class=751&version=script).
Behavioral and in-game validation of the new stored-object/live-contract path
are pending. No live game or account values
have been changed by preparing this feature. After explicit deployment, compare
an assessment with the selected farm's balance and loan, its native equipment
sale values, buildings and parcel prices, and at least one known stock quantity
and price. Check that goods still in a vehicle/implement or loose pallet are
excluded, then unload into building storage and compare the resulting credit.
Compare livestock counts and current prices with the native animal menu,
including different ages, an empty barn, and a newborn with a zero sale price.
Review any coverage gaps before treating its net as a closing figure.
For object storage, compare actual liters with the storage menu/save (including
a partial bale/pallet), check an empty storage, and confirm mission/foreign
contents are not credited. For native contracts, compare zero active missions,
a running mission, and a finished mission awaiting collection; an old accepted
event absent from the live list must remain historical without blocking the
preview. Stop any storage unloading before requesting the final estimate.
Verify the recipient and that the current SiN balance is displayed once, then
confirm both assets and balances are unchanged after requesting the report.
