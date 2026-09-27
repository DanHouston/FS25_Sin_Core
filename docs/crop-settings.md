# SiN FS25 Crop Settings

`SiN_FS25_Crop_Settings.zip` is a standalone multiplayer mod. It is deliberately
separate from `FS25_SiN_Server` and from every map. The shipped
`config/fruit-policy.xml` contains the first deliberately narrow production
probe: only the native `SORGHUM` fruit is enabled (`policyVersion="0.2.0-sorghum"`).
No other crop has an entry, so every other map fruit remains untouched.

## Runtime boundary

The script wraps `FruitTypeManager:loadMapData(xmlFile, missionInfo,
baseDirectory)`. It calls the native method first, then reads the external
policy and intersects it with `g_fruitTypeManager:getFruitTypes()` (falling back
to `getFruitTypeByName`). No fruit type, foliage layer, or map asset is created.
Each configured name is normalized to an upper-case FS25 fruit key. Unsupported
or malformed descriptor fields are skipped with bounded diagnostics; the
remaining policy continues fail-closed. A manager-local policy-version marker
prevents repeated mutation during one map load.

The runtime fields intentionally used by the policy are validated before any
write:

- `fruitType.growthDataSeasonal.periods[index].plantingAllowed`;
- `fruitType.growthDataSeasonal.periods[index].harvestAllowed`, when that
  native descriptor exposes it; and otherwise the fruit's native
  `getIsHarvestableInPeriod(growthMode, seasonPeriod)` method is wrapped for
  the configured fruit only. The wrapper changes the calendar-period gate but
  leaves native growth states and `getIsHarvestReady` unchanged, so an
  immature field cannot be harvested merely because the month is allowed.
- `fruitType.growthDataSeasonal.periods[index].growthTime`, when the native
  descriptor exposes a numeric timing value;
- `fruitType.growthDataSeasonal.periods[index].growthMapping[fromState]`, when
  that native mapping is present.

The Sorghum probe intentionally changes only planting and harvest periods:

- planting: `MID_SPRING`, `LATE_SPRING` (April-May);
- harvest: `LATE_SUMMER`, `EARLY_AUTUMN`, `MID_AUTUMN`, `LATE_AUTUMN`
  (August-November).

- The native `EARLY_AUTUMN` `harvestReady -> dead` transition is replaced with
  `harvestReady -> harvestReady`; otherwise the crop would wither before the
  requested November end date.
- No `growthTime` is changed, and no other Sorghum transition is changed.

Native Sorghum transitions still determine when a field reaches a harvest-ready
state; the policy only holds that state through the requested window and gates
harvesting to those periods. A policy change requires a normal map/save reload;
the script does not continuously overwrite native state.

Growth updates may use native state names (`startState`/`endState`) or validated
numeric state IDs. Names are resolved against the active fruit descriptor and
unresolved states fail closed.

The reviewed schema for a future entry is:

```xml
<fruit name="WHEAT" enabled="true">
  <seasonal>
    <period name="EARLY_SPRING" plantingAllowed="false"
            harvestAllowed="true" growthTime="2.5">
      <growth><update fromState="2" toState="4"/></growth>
    </period>
  </seasonal>
</fruit>
```

The shipped policy is the Sorghum probe above. Future entries must be added
deliberately and reviewed against the active map's native fruit descriptor.

## Deployment and verification

Server and every client must use the exact same ZIP bytes in multiplayer. The
release build publishes `SiN_FS25_Crop_Settings.zip` and records its SHA-256 in
`build-manifest.json` and `SHA256SUMS.txt`. Add that ZIP to the approved modpack
alongside the server ZIP, then publish the pack through the normal modpack
workflow. Do not edit a map ZIP to apply this policy.

For the Sorghum live probe, build the ZIP, install the same bytes on server and
every client, and restart/reload the save (policy application is map-load
scoped). Verify one bounded log line like:

`[SiN Crop Settings] policy=0.2.0-sorghum map=<map> applied=1 changed=1 skipped=0 unsupported=0 conflicts=0`

Then inspect the in-game calendar: Sorghum must show Plant Apr-May and Harvest
Aug-Nov, while Wheat (and another unchanged crop) must retain its existing
calendar. Existing crop density states are not rewritten; changing future
calendar transitions should therefore be validated against a backup of the
save.

This layer cannot supply missing fruits, foliage layers, density maps, map
terrain, productions, sell points, or map-specific scripts. Those remain map
integration work.
