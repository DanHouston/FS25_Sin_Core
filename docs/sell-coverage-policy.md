# Sell-point coverage policy

`SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua` runs during map/server
loading. It uses `g_fillTypeManager` to find economically sellable fill types
and `g_currentMission.storageSystem.unloadingStations` to audit actual selling
stations (`isSellingPoint == true`). Utility/farm-input fill types are
excluded by default; `excludedFillTypes` can be changed in the module.

Before the first selling station loads, the policy scans the active savegame's
placeable list, resolves each NPC station through FS25's live store and fill
type managers, and builds a complete coverage plan. It then augments only
selected stations' in-memory XML immediately before native
`SellingStation:load`. Explicit fill type triggers gain a name; category based
triggers gain a station-specific in-memory category. Both gain a native
`<fillType priceScale="1.0">` entry. FS25 creates its accepted types, price
dynamics, totals and trigger state through the normal load path. Source map,
savegame and other mod XML files are never changed.

The preflight must find a complete active placeable list. If it cannot, the
policy leaves assignments unresolved and logs
`assignment-preflight-unavailable`. A later native buyer found in the list
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

Example startup output:

```
[SiN Policy] Sell-point coverage audit
[SiN Policy] sellCoverage fillType=PEA index=41 buyers=1 class=BULK status=COVERED
[SiN Policy] ASSIGNED fillType=PEA station="Farmers Market" class=BULK priceScale=1.0
[SiN Policy] UNRESOLVED fillType=COTTON reason=no-compatible-NPC-trigger
[SiN Policy] eligible=72 covered=70 missing=2
[SiN Policy] coverage complete remainingMissing=2
```

The Lua load-sequence tests exercise a later native buyer, a missing bulk
buyer, category trigger assignment, physical incompatibility, private
ownership, an explicit override and preflight failure. The requested map-specific
commodities (MILK, GOATMILK, PEA, GREENBEAN, SPINACH, CARROT, COTTON,
RICELONGGRAIN, OLIVE, PARSNIP, POTATO, BEETROOT, RICE, SUGARBEET, SUGARCANE)
are inputs to this generic path, not hard-coded assignment rules. The runtime
audit is the authoritative validation for that installed map. Multiplayer
client price visibility and physical unloading still require a live server
test before this assignment path can be considered validated.
