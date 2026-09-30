# SiN FS25 Crop Settings

`SiN_FS25_Crop_Settings.zip` is a standalone multiplayer mod. It is deliberately
separate from `FS25_SiN_Server` and from every map. The shipped
`config/fruit-policy.xml` is the canonical SiN calendar (`policyVersion="0.3.3-native-harvest-range-guard"`).
Only fruit types registered by the active map are touched; absent map fruits are
reported as skipped and are never fabricated.

## Runtime boundary

The script wraps `FruitTypeManager:loadMapData(xmlFile, missionInfo,
baseDirectory)`. It calls the native method first, then reads the external
policy and intersects it with `g_fruitTypeManager:getFruitTypes()` (falling back
to `getFruitTypeByName`). No fruit type, foliage layer, or map asset is created.
Each configured name is normalized to an upper-case FS25 fruit key. Unsupported
or malformed descriptor fields are skipped with bounded diagnostics; the
remaining policy continues fail-closed. A manager-local policy-version marker
prevents repeated mutation during one map load.

The runtime uses the native fruit descriptor produced by
`FruitTypeDesc:loadGrowth()` rather than inventing a second calendar API. The
policy validates that descriptor and mutates its seasonal period gates and
growth mappings in place; the policy's `harvestAllowed` attribute is projected
onto the native `fruitType.growthDataSeasonal.periods[index].isHarvestable`
field. Native growth states and maturity checks remain authoritative, so an
immature field cannot be harvested merely because the month is allowed. A
missing seasonal table, period, mapping, state, or native harvest flag fails
closed for that fruit and is reported through bounded diagnostics. Perennial
entries deliberately leave their native mappings unchanged.

## Canonical calendar

FS25's twelve seasonal periods map to months as follows: `EARLY_SPRING` = March,
`MID_SPRING` = April, `LATE_SPRING` = May, `EARLY_SUMMER` = June,
`MID_SUMMER` = July, `LATE_SUMMER` = August, `EARLY_AUTUMN` = September,
`MID_AUTUMN` = October, `LATE_AUTUMN` = November, and `EARLY_WINTER` = December
(`MID_WINTER` and `LATE_WINTER` are January and February).

The policy windows are:

| Runtime fruit | Plant | Harvest |
| --- | --- | --- |
| BARLEY | Sep-Oct | Mar-Jun |
| CANOLA | Sep-Oct | Mar-Jun |
| CARROT | Apr-May | Aug-Nov |
| MAIZE | Apr-May | Sep-Dec |
| COTTON | Mar-Apr | Sep-Dec |
| GRAPE | Mar-Apr | Aug-Nov |
| GRASS | Mar-Nov | Mar-Dec |
| GREENBEAN | Apr-May | Aug-Nov |
| RICELONGGRAIN | Apr-May | Aug-Nov |
| OAT | Mar-Apr | Jul-Oct |
| OILSEEDRADISH | Mar-Oct | All months (calendar flag; not a combine crop) |
| OLIVE | Mar-Apr | Sep-Dec |
| PARSNIP | Apr-May | Aug-Nov |
| PEA | Mar-Apr | Jul-Oct |
| POPLAR | Mar-Oct | All months |
| POTATO | Mar-Apr | Jul-Oct |
| BEETROOT | Apr-May | Aug-Nov |
| RICE | Apr-May | Aug-Nov |
| SORGHUM | Apr-May | Aug-Nov |
| SOYBEAN | Apr-May | Sep-Dec |
| SPINACH | Mar-Apr | Jun-Nov |
| SUGARBEET | Mar-Apr | Sep-Dec |
| SUGARCANE | Mar-Apr | Mar-Dec |
| SUNFLOWER | Apr-May | Aug-Nov |
| WHEAT | Oct-Nov | Apr-Jul |

Annual crops use their validated native state names to rebuild only the seasonal
state mappings needed for the requested window. `stateChain` is an ordering hint,
not a promise that every visual state is present or used by every map. Before the
rewrite, the runtime derives the longest native path from the active descriptor;
optional states are therefore omitted when the map uses a direct transition (for
example `harvestReadyGreen -> harvestReady3`). Each period advances at most one
native stage; the terminal transition to the final harvest-ready state is withheld
until the harvest window, then the ready state is held through the window and
mapped to the native dead state in every subsequent period. This preserves
staggered maturity without inventing map stages or
changing `growthTime`.

The wither guard also reads the native fruit descriptor's
`minHarvestingGrowthState`/`maxHarvestingGrowthState` range. If a map exposes
multiple harvest-ready foliage states that are not named in the concise
`stateChain` (as some oat descriptors do), every registered state in that
range is held during the harvest window and mapped to the native dead state
afterward.

Every policy-owned seasonal descriptor is also totalized before it is handed
back to `GrowthSystem`: each numeric state exposed by the native descriptor
gets an integer self-transition unless the policy supplies a transition for
that period. This prevents FS25's `setCropsGrowthNextState` call from receiving
`nil` for an optional/map-specific foliage state while preserving native
states that the policy does not control. A non-integer native mapping fails
closed for that fruit before any gate or mapping is changed.

For an `annual` entry, `preserveNative="true"` means **skip calling
`FruitTypeDesc:loadGrowth` on the empty policy `<seasonal/>`**. It does not mean
keep the original seasonal transitions. `buildAnnualLifecycle` prepares all
twelve replacement `growthMapping` tables from the map's native path and the
configured windows. `applyFruit` then installs those mappings in place and sets
both `plantingAllowed` and `isHarvestable`. Without `lifecycle="annual"`, the
preserve-native entries retain their original growth mappings.

Before any annual mutation, each planting period is simulated from `invisible`
to the final ready state, through the last harvest period and into `dead`.
Missing initial/final states, a missing native path, malformed descriptors, or
a cohort that cannot finish in the window reject the entire fruit policy; the
original flags and mappings remain intact. There is no fallback that invents a
path merely because the state names exist. Intermediate stages follow native
edges; final readiness is held until the allowed window. The generated schedule
does not preserve the map's original monthly timing or winter pauses: those
timings are replaced by the configured SiN annual cycle.

### Executable lifecycle verification

Install test dependencies with `python -m pip install -r requirements-test.txt`,
then run `python -m unittest tests.test_crop_settings tests.test_crop_settings_lua`.
The Lua 5.1 harness executes the shipped script and XML through the map-load hook,
with adapters for the GIANTS XML/fruit registry interfaces. It tests both sowing
months for each of the following annual crops, using full native paths and
shortened paths where the reference descriptors skip optional visual states:

| Annual crop | Tested sowing periods | Ready state retained through | Withers in |
| --- | --- | --- | --- |
| Barley | Sep, Oct | Jun | Jul |
| Canola | Aug, Sep | Jun | Jul |
| Carrot | Apr, May | Nov | Dec |
| Maize | Apr, May | Dec | Jan |
| Cotton | Mar, Apr | Dec | Jan |
| Green bean | Apr, May | Oct | Nov |
| Long grain rice | Apr, May | Nov | Dec |
| Oat | Mar, Apr | Oct | Nov |
| Parsnip | Apr, May | Nov | Dec |
| Pea | Mar, Apr | Sep | Oct |
| Potato | Mar, Apr | Oct | Nov |
| Red beet | Apr, May | Nov | Dec |
| Rice | Apr, May | Nov | Dec |
| Soybean | Apr, May | Dec | Jan |
| Sugar beet | Mar, Apr | Dec | Jan |
| Sunflower | Apr, May | Nov | Dec |
| Wheat | Oct, Nov | Jul | Aug |
| Sorghum | Apr, May | Nov | Dec |

Every tested cohort must traverse the selected path, first reach maturity within
the crop's configured harvest window, remain ready until that window ends, then
enter the native dead state in every subsequent post-window period.
Different planting dates can converge on the same maturity period if both reach
the pre-maturity state before the window opens. Tests also prove rejection is
atomic and a repeat map-load hook does not reapply the policy. These are executable
descriptor tests; density-map scheduling and actual harvesting remain live FS25
validation responsibilities. Perennial/regrowth entries are not claimed to pass
this annual-cycle test.

Grass, Poplar, Sugarcane, Grapes, Olives and Spinach are treated as native
perennial/regrowth lifecycles: SiN changes their planting and harvest gates while
leaving map-owned growth transitions intact. The current user-edited policy sets
Oilseed Radish's calendar harvest flag for all twelve months; it remains a native
cover crop, not a newly enabled combine crop. These native
lifecycles cannot be safely reconstructed from a generic annual state chain.

Source XML changes do not update an installed ZIP. Rebuild and copy
`dist/SiN_FS25_Crop_Settings.zip` to the game's configured mod directory, then
fully reload the save. Version 0.2.3.0 packages the user-edited XML unchanged.
The policy version string alone cannot distinguish edits made without bumping
that string: compare the ZIP's `config/fruit-policy.xml` bytes to the source.
The reported Oilseed Radish visual glitch is not yet reproduced or resolved;
whether it concerns calendar bars or field foliage still needs confirmation.

### Growth boundary correction (mod 0.2.5.0)

The single-player Pea test showed `setMonthEngineState(8)` while entering
November and `setMonthEngineState(9)` while entering December. Peas withered
only in December under 0.2.4.0. Our former tests incorrectly applied a month's
mapping on entry to that same month. Native processing applies the outgoing
period's mapping at the next boundary.

Generated annual established-growth mappings are now scheduled one slot earlier;
planting eligibility and calendar harvest flags are unchanged. Germination stays
in the outgoing sowing month's mapping so the last sowing cohort is not stranded.
Cohort validation starts with an invisible crop planted during the sowing month,
then runs outgoing mappings at each following boundary. Every supported cohort
must mature inside its window, remain ready through the last allowed month, and
wither on the boundary into the following month. Explicit/native perennial
mappings are not shifted. No density-map state is forcibly rewritten.

The corrected model rejects the synthetic full seven-stage Sunflower path for
late sowing: it cannot finish by November at one stage per boundary. The shorter
native Sunflower path remains supported. Actual map support is checked at load;
look for unsupported diagnostics. Rice's missing-state limitation and Spinach's
preserved-native lifecycle remain unresolved and are not fixed by this change.

Live retest: install 0.2.5.0, reload before the end of a harvest window, run
`sinCropGrowth PEA`, then cross October to November. Period 8 should now show
`5>6`, with Peas withering in November, not December. Check Barley/Canola June
to July and Wheat July to August. Retest fresh early/late sowing cohorts too;
the maturity correction still needs live validation. Preserve the user-edited
XML bytes when rebuilding; do not shift calendar month names in that file.

### Read-only live withering probe (introduced in mod 0.2.4.0)

Descriptor tests do not prove density-map execution or NPC field behavior.
No field state is forcibly rewritten by this diagnostic.

Run `sinCropGrowth OAT` in the local single-player console (substitute the
affected registered fruit name). It records all twelve runtime seasonal
harvest flags and harvest-state successors, plus native `witheredState` and
the named `DEAD` state. Period 1 is March; period 9 is November. For oats the
configured July–October window now yields a withering successor in outgoing
period 8, which executes on entering November (period 9).
The command also arms four observations immediately before native
`GrowthSystem:setMonthEngineState` calls. Advance across a month boundary and
collect `[SiN Crop Settings] growth-probe` log lines, along with crop, month,
field number and player/NPC ownership. This establishes whether the mapping
is still present at the native boundary; it does not claim the engine has
changed a field. The wrapper forwards all arguments and return values and
does not invoke growth itself. No polling or continuous logging is added.

Sorghum uses the same annual `stateChain` policy as the other annual crops.
Its active map descriptor supplies the native `greenSmall -> greenMiddle ->
greenBig -> harvestReady` path; the policy retimes that path to April-May
planting, August-November harvest, and a native `harvestReady -> dead`
transition after the window.

A policy change requires a normal map/save reload; the script applies once after
`FruitTypeManager:loadMapData` and does not continuously overwrite runtime state.

Growth updates may use native state names (`startState`/`endState`) or validated
numeric state IDs. Names are resolved against the active fruit descriptor and
unresolved states fail closed.

The policy uses a compact, versioned shape. Annual entries name their stable
state chain and period windows; explicit `<seasonal>` updates remain available
for map-specific crops whose native lifecycle needs a hand-reviewed shape:

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

Future entries must be reviewed against the active map's native fruit
descriptor. A map that does not register one of the configured fruits simply
skips that entry.

## Deployment and verification

Server and every client must use the exact same ZIP bytes in multiplayer. The
release build publishes `SiN_FS25_Crop_Settings.zip` and records its SHA-256 in
`build-manifest.json` and `SHA256SUMS.txt`. Add that ZIP to the approved modpack
alongside the server ZIP, then publish the pack through the normal modpack
workflow. Do not edit a map ZIP to apply this policy.

For a live validation, build the ZIP, install the same bytes on server and every
client, and restart/reload the save (policy application is map-load scoped).
Verify one bounded log line like:

`[SiN Crop Settings] policy=0.3.3-native-harvest-range-guard map=<map> applied=<n> changed=<n> skipped=<n> unsupported=0 conflicts=0`

Then inspect the in-game calendar against the table above. Existing crop
density states are not rewritten; changing future calendar transitions should
therefore be validated against a backup of the save.

This layer cannot supply missing fruits, foliage layers, density maps, map
terrain, productions, sell points, or map-specific scripts. Those remain map
integration work.
