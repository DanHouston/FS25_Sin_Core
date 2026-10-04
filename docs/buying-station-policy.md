# Shared buying-station policy

`SiN_FS25_Policy/scripts/SiNBuyingStationPolicy.lua` recognizes all three
placeables in the configured Multifruit Buying Station mod. Both buying-only
versions (40% and 90%) are restricted to admins and, during the native
`PlaceableBuyingStation:onLoad` path, their in-memory buying trigger and price
rows are narrowed to `FERTILIZER`, `LIQUIDFERTILIZER`, `LIME`, and `SEEDS`.
Each version retains its own source price scales. The multipurpose version is
rejected for everyone because it includes a separate goods-selling area.
Other mods and source ZIP/XML files are left unchanged. Native FS25 loading
still constructs permitted stations, triggers and network state.

For multiplayer purchasing, the policy checks the authenticated incoming
`BuyPlaceableData` connection on the server using the GIANTS `UserManager`
and `User:getIsMasterUser()` APIs. A non-admin request for this exact station
is marked invalid before the native purchase validation. Other placeables and
admin requests use the native path. If the server lacks the verified hook, the
policy logs a warning; it does not claim the restriction is active.

Existing copies are not removed. Restart the dedicated server after installing
the updated Policy ZIP; the placed 90% station will be filtered when it loads.
All clients need the same SiN Policy version as the server. Runtime tests cover
the station allowlist, category removal, unrelated placeables, and admin vs.
non-admin purchase validation. Final purchase/unload behavior still needs an
in-game dedicated-server check.

Startup diagnostics are compact:

```
[SiN Policy] buyingStation ready loadHook=true purchaseGuard=true enabled=true adminOnly=true buyingVariants=2 multipurpose=disabled
[SiN Policy] buyingStation filtered target=.../multifruitstation_real.xml products=FERTILIZER LIQUIDFERTILIZER LIME SEEDS priceScales=FERTILIZER:0.90,LIQUIDFERTILIZER:0.90,LIME:0.90,SEEDS:0.90
[SiN Policy] buyingStation rejected non-admin purchase request
```
