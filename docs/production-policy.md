# SiN FS25 Production Policy

`SiN_FS25_ProductionPolicy` is a standalone runtime policy mod. It does not
alter map files, saves, or third-party ZIPs. The first policy is deliberately
narrow: it identifies the Lime Production store item as
`FS25_LimeProduction:LimeProduction.xml` and changes only its purchase price
from its source value of `$110,000` to its effective SiN price of `$500,000`.

## Canonical identity and discovery

The canonical ID is `<FS25 mod name>:<XML path relative to that mod root>`,
with `/` separators. The source mod name comes from
`Utils.getModNameAndBaseDirectory(storeItem.xmlFilename)`, not from the
localized display name. A descriptor is created only when the registered store
item is a `StoreSpecies.PLACEABLE` whose XML root is
`<placeable type="productionPoint">`.

Each descriptor retains `modName`, `xmlPath`, `sourcePrice`, `effectivePrice`,
recipes (inputs/outputs, cycles per hour, active-hour cost), and operating cost.
Unknown or malformed assets do not produce a descriptor and retain native
behavior. For an explicitly matched production, the runtime store item's price
is set to its effective value so ConstructionScreen's catalog (which renders
that field directly) displays the policy price. The native source value remains
separately retained in the immutable descriptor and metadata fields.

## Policy and runtime seam

The data file is `config/production-policy.xml`. Overrides must be positive
whole dollars; invalid entries fail closed. Applying the policy is idempotent:
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

## Diagnostics

`log.txt` records policy load, discovery, source price, policy match, effective
price, and applied native pricing calls. `sinProductionPolicy` logs retained
descriptors including recipes and operating costs. Diagnostics are bounded for
unknown productions; only configured pricing is logged at application time.

## Known limits

This changes new-construction purchase pricing. It does not retroactively alter
the value of an already placed production, resale economics, configuration
surcharges, terrain/placement costs, or production recipes. FS25 updates could
change the native price seam; the live purchase test below is mandatory for
every game update.

## Live validation

1. Keep `FS25_LimeProduction.zip` byte-for-byte unchanged; record its SHA256.
2. Install `SiN_FS25_ProductionPolicy.zip` beside it and enable both on a new
   test save (identical policy ZIP on a dedicated server and all clients).
3. Open Construction > Productions and select Lime Production. Confirm `$500,000`.
4. Purchase it on a farm with sufficient funds. Confirm the farm loses exactly
   `$500,000`, plus separately shown native placement/displacement costs only.
5. Run `sinProductionPolicy`; confirm source `110000`, effective `500000`, and
   canonical ID `FS25_LimeProduction:LimeProduction.xml`.
6. Save, reload, and repeat the store display and a new purchase test. Confirm
   unrelated production prices are unchanged.
7. Recalculate the original Lime ZIP hash and confirm it matches step 1.
