# SiN FS25 Policy

`SiN_FS25_Policy` is the single deployable policy mod. Its modules remain
independent within one FS25 package:

- `SiNProductionPolicy.lua` owns production prices, recipes, and construction
  catalog rules.
- `SiNVehiclePricingPolicy.lua` owns third-party motor-vehicle prices.

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
