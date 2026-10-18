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
+ harvested inventory / products value
- native farm loan
= estimated net farm equity

Proposed new account credit = floor(max(0, net farm equity))
Projected account balance = existing SiN account balance + proposed credit
```

The existing SiN balance is displayed separately and is never included in the
new credit. Negative equity shows an uncovered debt amount; it does not debit
the recipient or authorize a farm closure. Currency calculations use Decimal
in Central and the proposed credit rounds down to whole game-currency units.

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
- **Harvested goods:** current positive fill levels in farm-owned registered
  storages, silo extensions, production storages, vehicle/container fill units,
  and loose bales/items exposing native fill-level/type APIs. Each physical
  storage/item object is enumerated once. Physical fill-unit backing values
  avoid virtual getters that mirror separately counted mounted pallets.
- **Stock prices:** highest readable current outside selling-station quote.
  If absent, a readable positive native economy price is a labeled fallback
  estimate. The CSV includes quantity, unit price, source location, value,
  valuation basis, and station/notes. Missing prices remain unknown.

As requested, standing crops are excluded. Forestry, ground heaps, vehicle
operating fuel, sale fees, and delivery costs are outside this first estimate.
Livestock and bunker silos are called out for separate review when present;
their unmeasured contents are not silently given a zero value. Native asset
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
cash/asset operations are listed. Accepted native contracts are also flagged
for their possible completion or cancellation liabilities. A report with gaps shows known subtotals and
an unavailable proposed credit. Missing values are not substituted with zero.
Older snapshots can show legacy known resale/land subtotals while awaiting the
updated inventory, but cannot produce a payout figure.

## Components and deployment

- **JiN/core source:** adds the command, calculation and CSV renderer. Restart
  JiN from the updated release so its normal command sync registers the command.
- **Agent:** parses the new inventory and carries its source timestamp in the
  existing authenticated snapshot. It also includes the prepared rotating-log
  changes. The updated restart/updater scripts include task recovery settings.
- **FS25_SiN_Server 0.2.2.24:** adds read-only inventory capture. Install the
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

## Validation status

Behavioral and in-game validation are pending. No live game or account values
have been changed by preparing this feature. After explicit deployment, compare
an assessment with the selected farm's balance and loan, its native equipment
sale values, buildings and parcel prices, and at least one known stock quantity
and price. Review any coverage gaps before treating its net as a closing figure.
Verify the recipient and that the current SiN balance is displayed once, then
confirm both assets and balances are unchanged after requesting the report.
