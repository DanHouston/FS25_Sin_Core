# SiN FS25 Crop Settings

`SiN_FS25_Crop_Settings.zip` is a standalone multiplayer mod. It is deliberately
separate from `FS25_SiN_Server` and from every map. The shipped
`config/fruit-policy.xml` contains the first deliberately narrow production
probe: only the native `SORGHUM` fruit is enabled (`policyVersion="0.2.1-sorghum"`).
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

The runtime uses the native `FruitTypeDesc:loadGrowth()` path rather than
inventing a second calendar API. Before calling it, the script validates the
active fruit descriptor and every configured state transition. The native
loader applies `plantingAllowed`, `growthMapping`, and any other supported
growth fields; the policy's `harvestAllowed` attribute is then projected onto
the native
`fruitType.growthDataSeasonal.periods[index].isHarvestable` field. Native
growth states and maturity checks remain authoritative, so an immature field
cannot be harvested merely because the month is allowed. A missing method,
seasonal table, period, mapping, state, or native harvest flag fails closed for
that fruit and is reported through bounded diagnostics.

The Sorghum probe intentionally changes only planting and harvest periods:

- planting: `MID_SPRING`, `LATE_SPRING` (April-May);
- harvest: `LATE_SUMMER`, `EARLY_AUTUMN`, `MID_AUTUMN`, `LATE_AUTUMN`
  (August-November).

- The native `MID_SUMMER` `greenBig -> harvestReady` transition is replaced
  with `greenBig -> greenBig`; otherwise the crop can visibly become
  harvest-ready in July, before the August window begins.
- The native `EARLY_AUTUMN` withering transition is replaced, and explicit
  holds are added for `MID_AUTUMN` and `LATE_AUTUMN`, so the crop remains
  `harvestReady` and harvestable through November.
- `EARLY_WINTER` transitions `harvestReady -> dead`, ending the window after
  November rather than leaving an unharvested crop ready indefinitely.
- No `growthTime` is changed, and all other native Sorghum transitions remain
  unchanged.

Native Sorghum transitions still determine when a field reaches a harvest-ready
state; the policy only holds that state through the requested window and gates
harvesting to those periods. A policy change requires a normal map/save reload;
the script does not continuously overwrite native state.

Growth updates may use native state names (`startState`/`endState`) or validated
numeric state IDs. Names are resolved against the active fruit descriptor and
unresolved states fail closed.

The reviewed schema for a future entry is the native `loadGrowth` shape:

```xml
<fruit name="WHEAT" enabled="true">
  <growth>
    <seasonal initialState="greenBig">
      <period name="EARLY_SPRING" plantingAllowed="false"
              harvestAllowed="true" growthTime="2.5">
        <update startState="greenBig" endState="harvestReady"/>
      </period>
    </seasonal>
  </growth>
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

`[SiN Crop Settings] policy=0.2.1-sorghum map=<map> applied=1 changed=1 skipped=0 unsupported=0 conflicts=0`

Then inspect the in-game calendar: Sorghum must show Plant Apr-May and Harvest
Aug-Nov, while Wheat (and another unchanged crop) must retain its existing
calendar. Existing crop density states are not rewritten; changing future
calendar transitions should therefore be validated against a backup of the
save.

This layer cannot supply missing fruits, foliage layers, density maps, map
terrain, productions, sell points, or map-specific scripts. Those remain map
integration work.
