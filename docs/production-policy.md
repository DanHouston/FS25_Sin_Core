# Production module — SiN FS25 Policy

The production module in `SiN_FS25_Policy` is a standalone runtime policy module. It does not
alter map files, saves, or third-party ZIPs. The policy identifies explicitly
named production store items. It currently applies the approved SiN
purchase-price plan to farm-supply, direct crop, downstream food/animal, and
American Silos assets. Greenhouses, energy/BGA, forestry, and
construction-material assets remain native until their separate economic
models are approved. Lime Production is
`FS25_LimeProduction:LimeProduction.xml`; its source price is `$110,000`
and its current SiN price is `$100,000`.

## Canonical identity and discovery

The canonical ID is `<FS25 mod name>:<XML path relative to that mod root>`,
with `/` separators. Base-game assets use
`FS25_BaseGame:data/<path>`. The source mod name comes from
`Utils.getModNameAndBaseDirectory(storeItem.xmlFilename)`, not from the
localized display name. A descriptor is created only when the registered store
item is a `StoreSpecies.PLACEABLE` whose XML contains a
`<placeable><productionPoint>` block; this supports production-capable
placeables without assuming one particular placeable type.

Each descriptor retains `modName`, `xmlPath`, `sourcePrice`, `effectivePrice`,
recipes (inputs/outputs, cycles per hour, active-hour cost), and operating cost.
Unknown or malformed assets do not produce a descriptor and retain native
behavior. For an explicitly matched production, the runtime store item's price
is set to its effective value so ConstructionScreen's catalog (which renders
that field directly) displays the policy price. The native source value remains
separately retained in the immutable descriptor and metadata fields.

## Policy and runtime seam

The data file is `config/production-policy.xml`. Purchase-price overrides must
be positive whole dollars; invalid entries fail closed. A production entry can
also hold narrowly targeted recipe rules: a positive `cyclesPerHour`, exact
named input/output amount overrides, or `enabled="false"` to remove a named
recipe from the native production indexes. Native recipe IDs may contain
letters, numbers, underscores, hyphens, and spaces (for example the American
Silos `forage mixer` recipe). Applying the policy is idempotent:
the original price is retained before the one matched catalog item is updated,
and repeated discovery does not compound the effective value.

The narrow interception point is `EconomyManager:getBuyPrice`. FS25 uses that
method when `BuyPlaceableData:updatePrice` creates its purchase data, therefore
it feeds both construction pricing and the server-side purchase calculation.
The hook returns the native value unchanged unless the store item’s canonical
production ID is explicitly configured. It deliberately does not globally
change `EconomyManager` prices, store item registration, farm balances, or
unrelated placeables.

The same immutable mod ZIP belongs on server and clients so the construction UI
matches the server. FS25's `BuyPlaceableData` stream does not carry a price;
after its server-side `readStream` resolves the store item/configurations, this
mod forces native `updatePrice()` again. The dedicated server therefore computes
the charge itself through the same `getBuyPrice` hook; a client does not supply
a policy-selected price.

### Runtime recipe seam

`ProductionPoint.load` is wrapped with `Utils.overwrittenFunction`. Native
recipe loading completes first; the mod then resolves the placeable's canonical
XML identity and applies only configured recipe IDs. At this seam FS25 has
created both the `productions` lookup used by simulation and the
`sortedProductions` list used by the production UI.

The current recipe policy is intentionally limited to:

- `FS25_LimeProduction:LimeProduction.xml`: purchase price `$100,000`; recipe
  `Lime`: `300` stone to `3,000` lime at one cycle per hour;
- `FS25_fertilizerProductionDS:xml/fertilizerProduction_DS.xml`: purchase
  price `$230,000`;
- `FS25_RH_LiquidFertillizerProduction:liquidFertilizerFactory.xml`, recipe
  `LiquidFertilizerFactory`: purchase price `$175,000`, one cycle per hour;
- `FS25_SeedProductionFactory:seedProductionFactory.xml`: purchase price
  `$300,000` and removal of the four explicitly named `*_rush` recipes. Normal
  seed recipes are unchanged;
- the four `FS25_AmericanSilosProductionPack` entries: small-tier purchase
  price `$302,500` with the common `4,000/1.5/2.5` hay/silage, pig-food, and
  forage rates; large-tier purchase price `$403,000` with the common
  `5,000/2/3.5` rates. Storage capacities remain native because no safe
  authoritative capacity seam is implemented.

The remaining approved purchase prices are data-only entries in
`config/production-policy.xml`; they do not change native input/output ratios
or recipe concurrency. This keeps the price rollout separate from any future
throughput or yield decision. The source production ZIPs remain untouched.

An unmatched runtime structure or input/output name fails closed for that
recipe and is logged. A map/save reload is required for a recipe policy to
reach an already placed production. The same policy ZIP must be installed on
the server and all clients; the dedicated server remains authoritative for the
actual production simulation.

## Diagnostics

`log.txt` records policy load, post-store-registration inventory discovery,
source price, policy match, effective price, and applied native pricing calls.
`sinProductionPolicy` logs retained
descriptors including recipes and operating costs. Diagnostics are bounded for
unknown productions; only configured pricing is logged at application time.

For an actual pricing review, run `sinProductionPolicyExport` on the
authoritative server (or a single-player test save). It writes the full,
canonical-ID-sorted catalog to:

```text
<FS25 user profile>/modSettings/SiN_FS25_ProductionPolicy/production-catalog.csv
```

The file has no timestamp or display-name fields, so it is safe to diff between
maps/mod sets. Its columns are canonical ID, originating mod/XML path, source
price, current effective price, recipe count, and active-hour operating cost.
The export is deliberately unavailable to multiplayer clients: policy review
uses the same authoritative catalog that governs purchases.

## Construction sell-point catalog policy

The same small policy mod also carries one narrow construction-catalog rule:
`config/construction-policy.xml` sets `sellingPoints showInConstruction="false"`.
After the store catalog has loaded, it identifies placeable XML containing
`<placeable><sellingStation>`, sets that item's shop `showInStore` value to
`false`, and removes its construction `brush` descriptor. `showInStore` alone
does not control ConstructionScreen; the brush descriptor is the native data
that puts a placeable in its construction tab. Some matched assets also have
production behavior; the policy intentionally excludes them because the rule
is that the Selling Points construction tab contains no purchasable entries.
It does not use a name or a localized category match, and does not alter
existing, preplaced, or map-owned sell points. `sinConstructionPolicy` reports
the exact catalog entries whose visibility this policy changed.

This is a catalog/UI rule, not an economy or save-state mutation. All clients
need the same mod so their construction catalogs agree; the authoritative
server owns the same configuration. Deliberately malformed or unavailable
construction policy files fail closed, leaving sell points visible.

## Known limits

This changes new-construction purchase pricing. It does not retroactively alter
the value of an already placed production, resale economics, configuration
surcharges, or terrain/placement costs. FS25 updates could
change the native price seam; the live purchase test below is mandatory for
every game update.

## Live validation

1. Keep every third-party production ZIP byte-for-byte unchanged; record the
   hashes of any assets being tested.
2. Install `SiN_FS25_ProductionPolicy.zip` beside them and enable both on a new
   test save (identical policy ZIP on a dedicated server and all clients).
3. Open Construction > Productions and spot-check Lime (`$100,000`), cereal
   (`$995,000`), an approved downstream line, and both silo tiers (`$302,500`
   small / `$403,000` large). Hold-native categories should retain their
   source prices.
4. Purchase it on a farm with sufficient funds. Confirm the farm loses exactly
   `$100,000`, plus separately shown native placement/displacement costs only.
5. Place Lime Production, open its production menu, and confirm its only recipe
   requires `300` stone and produces `3,000` lime at one cycle per hour.
6. Place Liquid Fertilizer Factory and confirm its recipe rate is one cycle per
   hour. Place Seed Production Factory and confirm none of the four `rush`
   recipes appears or can be activated.
7. Run `sinProductionPolicy`; confirm source `110000`, effective `100000`, and
   canonical ID `FS25_LimeProduction:LimeProduction.xml`.
8. Save, reload, and repeat the recipe and store-display checks. Confirm
   unrelated production prices and recipes are unchanged.
9. Recalculate the original production ZIP hashes and confirm they match step 1.
