# SiN FS25 Crop Settings

`SiN_FS25_Crop_Settings.zip` is a standalone multiplayer mod. It is deliberately
separate from `FS25_SiN_Server` and from every map. The shipped
`config/fruit-policy.xml` is an empty, versioned policy (`schemaVersion="1"`,
`policyVersion="0.1.0"`), so the initial package does not change crop behavior.

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
  native descriptor exposes it;
- `fruitType.growthDataSeasonal.periods[index].growthTime`, when the native
  descriptor exposes a numeric timing value;
- `fruitType.growthDataSeasonal.periods[index].growthMapping[fromState]`, when
  that native mapping is present.

The final crop calendar is not selected yet. A policy change requires a normal
map/save reload; the script does not continuously overwrite native state.

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

This is documentation only; the shipped policy contains no enabled fruit.

## Deployment and verification

Server and every client must use the exact same ZIP bytes in multiplayer. The
release build publishes `SiN_FS25_Crop_Settings.zip` and records its SHA-256 in
`build-manifest.json` and `SHA256SUMS.txt`. Add that ZIP to the approved modpack
alongside the server ZIP, then publish the pack through the normal modpack
workflow. Do not edit a map ZIP to apply this policy.

For the first live probe, use a disposable policy entry in a staging copy of
`config/fruit-policy.xml` for a fruit known to exist on the target map (for
example, one `plantingAllowed` period). Build one test ZIP, install the same
bytes on server and client, restart/reload the save, and verify one bounded log
line like:

`[SiN Crop Settings] policy=test-1 map=<map> applied=1 changed=1 skipped=0 unsupported=0 conflicts=0`

Then inspect the in-game calendar and planting behavior. Restore the empty
production policy before publishing the normal release. Existing crop density
states are not rewritten; changing future calendar transitions should therefore
be validated against a backup of the save.

This layer cannot supply missing fruits, foliage layers, density maps, map
terrain, productions, sell points, or map-specific scripts. Those remain map
integration work.
