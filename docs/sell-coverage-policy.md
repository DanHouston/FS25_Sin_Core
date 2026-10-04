# Sell-point coverage policy

`SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua` runs during map/server
loading. It uses `g_fillTypeManager` to find economically sellable fill types
and `g_currentMission.storageSystem.unloadingStations` to audit actual selling
stations (`isSellingPoint == true`). Utility/farm-input fill types are
excluded by default; `excludedFillTypes` can be changed in the module.

Before the first selling station loads, the **server** scans the active
placeable XML object that FS25 has already opened for its own load (it does
not open a hard-coded savegame path). It resolves NPC stations through the
live store and fill-type managers, then builds a complete coverage plan. It
augments only selected stations' in-memory XML immediately before native
`SellingStation:load`. Explicit fill type triggers gain a name; category-based
triggers gain a station-specific in-memory category. Both gain a native
`<fillType priceScale="1.0">` entry. FS25 creates accepted types, price
dynamics, totals and trigger state through its normal load path.

On a dedicated-server join, Policy prefixes each placeable's ordinary network
stream with the assignments the server actually applied to that placeable.
The joining client reads this **before** its asynchronous placeable load, then
applies the same in-memory XML additions before its own native
`SellingStation:load`. The client never reads the server's savegame files or
decides which station should be a buyer. Source map, savegame and other mod
XML files are never changed. If the server cannot see FS25's active placeable
list or cannot install the join-stream hook, multiplayer assignment fails
closed rather than creating a server-only buyer.

The preflight must find a complete active placeable list. If it cannot, the
policy leaves assignments unresolved and logs
`active-placeable-list-unavailable`. A later native buyer found in the list
prevents an earlier station from receiving a duplicate assignment. Player-owned
placeables are ignored. The post-load audit checks the live storage system.

Delivery classes are `BULK`, `LIQUID`, `BALE`, `PALLET`, and `SPECIAL`.
The live FS25 liquid category identifies tanker cargo, with a raw milk-name
fallback for milk fill types missing that category. FS25 marks raw milk as
both bulk and pallet-capable, so fallback assignment requires both a regular
unload trigger and a pallet trigger on the same NPC station. Dairy products
already bought there guide station preference. Root crops and raw vegetables
prefer stations already buying conventional bulk crops, such as grain
stations. Similarity only compares accepted fill types of the same delivery
class for crop scoring, so processed pallet names do not pull raw bulk crops
to a produce-pallet buyer. Explicit exclusions such as the map's `COTTON` bale
exclusion are respected. Similarity scoring prefers related commodities,
followed by deterministic filename order.
`overrides` takes priority over automatic scoring; incompatible overrides fail
closed.

For a commodity with no native NPC buyer, `fallbackBuyerCount=2` adds up to
two distinct compatible NPC buyers. The first uses the normal similarity
ranking. A second must also have a related native commodity
(`additionalBuyerMinSimilarity=20`), so a merely usable bulk trigger such as
the Debris Crusher is not added as an unrelated extra buyer. Equal scores are
resolved by station XML filename. An explicit `overrides` mapping remains an
exact single-station choice; `preferredStations` ranks that station first but
allows a qualified second buyer. Fill types with any native NPC buyer are
left unchanged. Each fallback uses the normal FS25 market price with
`priceScale=1.0`.

Example startup output:

```
[SiN Policy] Sell-point coverage audit
[SiN Policy] ASSIGNED fillType=PEA station="Grain Silo A" class=BULK priceScale=1.0
[SiN Policy] ASSIGNED fillType=PEA station="Grain Silo B" class=BULK priceScale=1.0
[SiN Policy] sellCoverage fillType=PEA index=41 buyers=2 stations="Grain Silo A,Grain Silo B" class=BULK status=COVERED
[SiN Policy] UNRESOLVED fillType=COTTON reason=no-compatible-NPC-trigger
[SiN Policy] eligible=72 covered=70 missing=2
[SiN Policy] coverage complete remainingMissing=2
```

The Lua load-sequence tests exercise a later native buyer, a missing bulk
buyer, category trigger assignment, physical incompatibility, private
ownership, an explicit override, preflight failure, and server-to-client
assignment streaming before native load (including both raw-milk trigger
paths). It also verifies that two related buyers are chosen deterministically
without pulling in an unrelated bulk trigger. The requested map-specific
commodities (MILK, GOATMILK, PEA, GREENBEAN, SPINACH, CARROT, COTTON,
RICELONGGRAIN, OLIVE, PARSNIP, POTATO, BEETROOT, RICE, SUGARBEET, SUGARCANE)
are inputs to this generic path, not hard-coded assignment rules. The runtime
audit is the authoritative validation for that installed map. A live
dedicated-server/client test is still required: check `streamHook=true` and
`ASSIGNED` on the server, then verify the same buyer in the joining client's
Prices page and with a physical sale. Hosted local testing alone cannot
validate dedicated-server synchronization.
