# SiN FS25 Policy

`SiN_FS25_Policy` is the single deployable policy mod. Its modules remain
independent within one FS25 package:

- `SiNProductionPolicy.lua` owns production prices, recipes, and construction
  catalog rules.
- `SiNVehiclePricingPolicy.lua` owns third-party motor-vehicle prices.
- `SiNSellCoveragePolicy.lua` owns sell-point coverage. It reads
  the live fill-type and storage systems, excludes utility fill types by
  default, and uses native SellingStation initialization to add only
  physically compatible NPC buyers. The default fallback `priceScale` is 1.0;
  no fixed commodity price is introduced.

At startup it logs an audit (`sellCoverage fillType=PEA ...`) and a compact
summary. Assignments are injected into the in-memory XML immediately before
the native selling-station load, so accepted types, price visibility, dynamic
pricing, trigger state, and statistics use FS25's native load path.
No source map or third-party mod XML is written. A commodity without a same-
class NPC trigger is left `UNRESOLVED`/missing rather than receiving a
price-table-only entry. Policy knobs live at the top of
`SiNSellCoveragePolicy.lua`: `enabled`, `excludedFillTypes`, `overrides`,
`preferredStations`, `fallbackBuyerCount`, `additionalBuyerMinSimilarity`,
and `defaultPriceScale`. Missing commodities can receive two related NPC
fallback buyers; commodities with a native buyer are not expanded.
The server plans from FS25's already-open active placeable list, not a
savegame path. Applied assignments travel in the placeable join stream and
are injected before the joining client's native station load. If the active
list or multiplayer stream hook is unavailable, assignment fails closed.
Dedicated-server/client behavior still needs a live acceptance test.

The modules have separate XML policy files, registries, console diagnostics,
and runtime seams. They do not call each other or share mutable FS25 catalog
objects. Base-game vehicle prices are read-only anchors and are never changed.

Install `SiN_FS25_Policy.zip` and remove the legacy
`SiN_FS25_ProductionPolicy.zip` and `SiN_FS25_Vehicle_Pricing_Policy.zip` from
the active mod folder. Running both generations would apply the same policies
twice.

Production prices use matched placeable catalog prices and native server-side
placeable recalculation. Vehicle prices change only matched third-party vehicle
catalog prices; neither module wraps `EconomyManager:getBuyPrice`, because that
global shop method is also used by vehicle configuration UI.
