# SiN Harvest field markers

The SiN Server mod hides only the in-game field-number marker for fields whose
associated farmland is currently owned by the farm named `SiN Harvest` (name
matching ignores case, spaces, and punctuation). It uses native field-definition
hotspots when exposed by the map; otherwise it hooks the client UI's
`IngameMapElement.draw` and inspects the actual `self.ingameMap.hotspots` before
FS25 draws them. Only native `FarmlandHotspot` entries are eligible, mapped via
`getFarmland()`. `g_currentMission.ingameMap` is not the PDA/minimap map object
in this client build, so polling it from the mod update loop cannot work. The
fallback covers labeled plots omitted from `FieldManager:getFields()`, including
wooded/non-crop areas.
Underlying farmland, field polygons, field-state layers, buildings, roads, and
sell-point objects remain unchanged.

This is a client-side presentation rule evaluated against the synchronized
farmland owner at most once per second while the PDA/minimap is drawn. It follows
purchases and sales without a save edit.
When ownership moves away from SiN Harvest, the marker's original visibility is
restored. If SiN Harvest cannot be resolved or neither marker source is
available, the code fails open and leaves/restores the normal markers. The
client log should first report installation of the `IngameMapElement.draw
farmland hotspot hook`, then include `source=fieldDefinitions` or
`source=ingameMap.hotspots` and inspected/hidden counts after opening the PDA or
minimap.

## Validation

Automated Lua-runtime coverage verifies that owned field markers hide, another
farm's markers remain visible, ownership transfer restores the marker, and the
field objects/polygons remain present. Before deploying to the dedicated server,
test in a multiplayer client: open the PDA and minimap; confirm every SiN
Harvest-owned field number disappears; confirm shops, roads, field-state layers,
and unowned/other-farm field numbers remain; then sell one test parcel or change
ownership and confirm its number returns. The mod must be present on clients for
the local map marker change to render there.
