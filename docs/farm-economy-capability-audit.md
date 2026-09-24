# Farm lifecycle and economy capability audit

This document records the boundary before any FS25 cash, loan, or destructive
farm mutation is enabled. The central ledger and the FS25 save are different
authorities; a Central request is never proof that game state changed.

## Source-backed findings

The official FS25 `FarmlandManager` documentation exposes
`getFarmlandById`, `getFarmlandOwner`, and `setLandOwnership`. Its FS25 source
uses `farmland.price` as the ownership-parcel price when calculating a
farmland-related purchase. SiN's starting-land workflow may therefore identify
the requested parcel by its farmland ID and must use that parcel's runtime
`price`, never field acreage or field geometry.

The official documentation also shows native FS25 code calling
`g_currentMission:addMoney(amount, farmId, moneyChangeType, true)`. This is
evidence that FS25 has a server money path, but it does **not** establish the
appropriate `MoneyType` for a SiN administrative adjustment, the exact
replication/finance-history behavior in the deployed dedicated build, or a
safe idempotency boundary around a direct call. SiN consequently does not
enable cash adjustments, deposits, withdrawals, or starting cash from this
observation alone.

`FarmManager:removeFarm` is documented as an internal consequence of farm
destruction. The player-facing FS25 deletion flow is documented as requiring
server administration and an empty farm. That does not establish a safe
dedicated-server mod call, nor the disposition of vehicles, placeables,
productions, storage, money, or loans. SiN therefore has no `/farm_remove`
mutation yet.

No FS25 source contract was found for an authoritative server-side loan setter
or delta operation. SiN must not synthesize a game loan or use a Central ledger
entry as a substitute for the requested FS25 loan.

Sources: [FS25 FarmlandManager](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=25&class=214&version=engine),
[FS25 source use of `farmland.price`](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=78&class=713&version=engine),
and [GIANTS FarmManager reference](https://gdn.giants-software.com/documentation_print.php).

## Required live capability probe

On a disposable dedicated save, instrument a read-only runtime probe first and
then test one operation at a time with exact before/after records:

1. Read a farm's current money and loan through the candidate runtime methods.
2. Identify the specific money change type accepted by the target FS25 build;
   perform one reversible adjustment and verify the finance screen, client
   replication, save, and restart.
3. Establish an authoritative loan mutation and read-back, including maximum
   loan and interest behavior.
4. On an asset-free disposable farm, establish the exact farm-deletion path and
   verify asset, farmland, membership, money, loan, and save behavior.

Only then may a receipt-gated FS25 economic operation be added. A reference
executor or a successful Lua `pcall` is not sufficient proof.

## World-marker failure boundary

The authoritative world marker is written into the save-backed
`FS25_SiN_Server_world.xml` by the actual `Mission00`/`FSBaseMission.saveSavegame`
lifecycle hook. FS25 supplies the temporary save directory to that hook and
promotes it with the completed save; this is not an in-memory
`FSCareerMissionInfo.saveToXMLFile` mutation. A readable marker is re-used
verbatim. A `careerSavegame.xml` marker from the previous release is accepted
only as a compatibility read path. If an existing marker is malformed, the
mod refuses all world-scoped operations; it does not silently generate a
replacement identity. This is deterministically reviewed from the mod path,
but the GIANTS save-hook write and restart behavior remain **LIVE VALIDATION
REQUIRED**. New identities remain fail-closed until the first successful save
has written and verified the marker. No claimed FS25 save UUID exists.

## Farm 14 roster evidence

`FS25SiNServer:exportSnapshot` iterates actual `g_farmManager:getFarmById(1..254)`
records and writes each one whose `farm.name` is non-`nil`. The current roster
uses that latest stored snapshot directly. Thus the captured `Farm 14: unnamed`
line establishes that the captured runtime snapshot contained a real Farm 14
whose name was an empty string; it is not generated from an unrelated Central
record or a Python dictionary formatting error.

This does not establish *why* the runtime farm existed, whether the snapshot was
old, or whether it contained assets. Do not delete, rename, or otherwise mutate
Farm 14. The next live inspection should retain the snapshot timestamp, `Farm
14` user/member count, money/loan read-only observations, owned farmlands, and
the dedicated-server log around its creation.
