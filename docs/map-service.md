# SiN map service

The map service is a central-side, filesystem-free rendering boundary. It
accepts normalized map metadata and an already-loaded overview image, then
produces bounded deterministic PNGs for Discord presentation. It does not
change farm ownership, field availability, contracts, or authorization. The
same service can render field, farmland, ownership, and future arbitrary
geographic overlays without knowing anything about the consumer.

## What is established from FS25 sources

The authoritative runtime concepts are separate:

- `FarmlandManager` owns the farmland layer and exposes methods such as
  `getFarmlandById`, `getFarmlands`, `getFarmlandOwner`, and
  `getFarmlandIdAtWorldPosition`.
- `FieldManager` loads field definitions from the active map's field data and
  maintains the field collection and the field-to-farmland association.
- A field's `farmland.id` is the stable relationship used by current FS25
  scripts; a generic `getFieldByFarmland` helper must not be assumed.
- `setLandOwnership` changes live farmland ownership. Ownership is therefore
  save/runtime state and is not part of immutable field geometry.
- Map XML commonly identifies the PDA/overview image through an
  `imageFilename`-style reference. Base maps and mod maps can use different
  paths and formats, so SiN does not guess a filename or commit a GIANTS
  asset.

The centered world-to-map conversion documented by GIANTS is equivalent to:

```text
u = (world_x + world_width / 2) / world_width
v = (world_z + world_depth / 2) / world_depth
```

The renderer maps `u` to image X and, by default, maps `v` to inverted image Y
because a top-down raster's north/top is conventionally the low-Y edge. The
Y inversion is explicit in `MapModel.image_y_inverted`; it still requires
visual verification against each actual map PDA before production use.

The runtime source evidence is the [GIANTS FS25 FarmlandManager documentation](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=25&class=214&version=engine),
the [GIANTS FS25 FieldManager documentation](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=17&class=183&version=script),
and the current FS25 script pattern of iterating
`g_currentMission.fieldManager:getFields()` and matching `field.farmland.id`.
That field/farmland relationship is also called out by the [FS25 Seasonal Crop Stress implementation notes](https://github.com/Realistic-Farming/FS25_SeasonalCropStress/blob/main/CLAUDE.md).
Map XML overview references vary by map; the [map XML reference](https://alfamods.no/xml-reference/filename/filename-maps/)
is useful corroborating evidence but is not treated as a fixed runtime contract.
The repository intentionally does not copy proprietary scripts or map images.

## Current implementation

Each authenticated runtime geometry export receives a deterministic
`map_revision` from its normalized payload. Its event ID is stable for retries
within one FS25 runtime and includes a runtime nonce, so a later runtime can
refresh changed geometry. JiN compares the persisted revision with its
in-memory map before re-registering; opening another contract does not
invalidate or regenerate an unchanged map.

Automated tests cover nested XML parsing, authenticated Central validation and
persistence, JiN loading, and PNG rendering. FS25 extraction and final Discord
attachment delivery still require live validation.

`fs25_network_core.map_service` provides:

- versioned `MapModel`, `FieldGeometry`, and `FarmlandGeometry` records;
- multiple rings, including irregular fields and holes;
- field-to-farmland mapping without collapsing the two concepts;
- bounded validation for IDs, dimensions, points, and overlays;
- centered world-to-normalized/PDA pixel transforms;
- standard-library DDS decoding for bounded 32-bit, BC1/DXT1, and BC3/DXT5
  overview data;
- deterministic PNG encoding and field/farmland/ownership overlays;
- bounded LRU caches for decoded base maps and rendered overlays;
- `MapModel.to_dict()`/`from_dict()` as the versioned transport boundary.

`MapService.register_map(server_key, save_key, model, base_rgba=...)` or
`overview_dds=...` is deliberately an explicit ingestion boundary. A trusted
runtime geometry export may also call `register_payload(...)` without a raster;
that path uses a deterministic neutral canvas and never interprets a Discord
input as a filesystem path. The service does not read arbitrary Agent or
game-server files. `MapStore` is the single `sin_maps` persistence boundary:
it validates a stored payload on reload, scopes every record by server/save,
and replaces the map projection with one Mongo update. `MapService.load_persisted`
uses the same payload validation path that trusted ingestion uses, so a process
restart cannot expose malformed or partially loaded geometry.

The model records every observed farmland ID separately from farmland geometry.
FS25's authoritative farmland layer is a density map and the documented
`FarmlandManager` API exposes IDs, ownership, and world-position lookup, but
not a farmland polygon method. The runtime therefore exports all farmland IDs
and field polygons plus their `field.farmland.id` relationship. A farmland
polygon is accepted only when a trusted exporter supplies one; rendering a
farmland with no polygon fails safely as unavailable rather than displaying a
field polygon as a false parcel boundary.

Runtime exports include a monotonic mailbox `source_generation`. Central keeps
the newest generation and ignores delayed older exports. Equal-content
retransmission remains safe and consumers reload only when the normalized
`map_revision` changes. A changed map is persisted atomically before a
consumer can load it; an invalid incoming payload leaves the prior valid
projection in place through the Agent quarantine path.

The current FS25_SiN_Server runtime emits one authenticated `map_geometry`
event per loaded server session after binding. It contains map identity,
terrain dimensions, the supported centered-XZ coordinate-system declaration,
all known farmland IDs, field-to-farmland relationships, acreage where
available, and actual world-coordinate field polygon points. The Agent forwards
it through the existing `/api/server/events` transport. Central validates and
persists the normalized payload in `sin_maps`; JiN lazily loads that record
into MapService when publishing a contract card. It is not a continuous
telemetry stream.

## Contract cards

`NetworkBot` now carries an optional `MapService`. A contract records its
resolved server/save context when the creator has one unambiguous eligible
game context. Publishing a contract card attempts to render the requested
numeric field IDs and attaches `sin-map.png` when a validated map is
registered. A missing map, unsupported image, unknown field, or render error
only produces a text-only card; the durable contract is already created and
is never rolled back because presentation failed.

The jobs channel remains deployment configuration (`channels.jobs` in
`discord.json`). No channel ID is embedded in business logic. Multiple fields
are passed to the renderer together so one card can highlight all requested
fields.

## Static versus live state

Static/slow-changing map data is model geometry, dimensions, map identity, and
the overview image identity. Live state is farmland ownership and future crop,
condition, vehicle, and player observations. Ownership overlays must be fed
from the authenticated server snapshot and must not be baked into static map
assets. Historical player coordinates are not stored by this service.

The bounded cache key includes server/save scope, map version, overview asset
identity, requested overlays, labels, and an ownership revision/value. A map
registration or explicit ownership revision invalidates the relevant render
cache; there is no unbounded PNG directory.

Server/save scope is part of both the persistence key and every render-cache
key. Two servers may use the same field or farmland number without sharing a
model, revision, or rendered image. Farmland selection is available through
`render_map(..., highlight_farmlands=[...])` when validated parcel geometry is
present; this is the same consumer-neutral API a future farm-request flow can
use, independent of ContractService.

As a local synthetic reference, rendering a 512x512 RGBA fixture with one
irregular field overlay completed in roughly 76 ms on the development host;
this is a smoke measurement, not a production performance guarantee.

## Security and limits

Map IDs and asset identities are validated; unsafe path components are
rejected. The renderer bounds image dimensions, total pixels, geometry points,
geometry records, and requested overlays. DDS headers, block lengths, and
pixel sizes are checked before decompression. Only bytes supplied through the
trusted registration boundary are decoded. Discord users cannot provide
filesystem paths or cause arbitrary files to be opened.

## Live extraction plan

The server-side exporter now performs the following steps at runtime:

1. identify the active map title/ID and terrain dimensions;
2. enumerate `g_currentMission.fieldManager:getFields()`, capturing each field's
   stable ID, `field.farmland.id`, acreage where available, and exact world
   polygon points;
3. enumerate `g_farmlandManager:getFarmlands()` for the separate farmland-ID
   layer. No vector parcel is fabricated because the supported FS25 API exposes
   the farmland density map rather than polygon points;
4. emit one versioned normalized `map_geometry` event through the existing
   authenticated mailbox transport;
5. validate and persist it in Central before JiN renders a contract card.

The exporter deliberately does not copy a PDA asset or expose a filesystem
path. When no trusted raster is registered, MapService draws the real runtime
polygons on a deterministic neutral canvas. Compare Field 22 and several
irregular fields against the in-game PDA before treating orientation as
production-verified.

## Runtime probe

On a stopped/testable FS25 server, run `sinSelfTest` from the FS25 server
console and retain the `Map probe` line with the server log. The probe reports
the loaded map title/identity, terrain size, field count, Field 22's
field-to-farmland relationship and available geometry metrics, polygon-point
count and world-coordinate bounds when exposed, farmland count,
farmland-map dimensions when exposed, and whether map asset references are
present. It is read-only and deliberately reports asset-reference presence
rather than exposing filesystem paths or copying proprietary assets. Normal
startup separately queues the authenticated geometry export; the probe itself
does not upload anything.

## LIVE-VALIDATION-REQUIRED

The local scenarios prove normalized parsing, authenticated ingestion, durable
reload, revision ordering, server/save isolation, coordinate math, farmland
overlay selection, and Discord-facing PNG/text fallback. The following checks
still require a real FS25 build and a human-observable test environment:

| Check | Procedure | Success evidence | Failure meaning |
| --- | --- | --- | --- |
| GIANTS field extraction | Load the target save, run `sinSelfTest`, and retain the map probe plus the emitted `map_geometry` event after startup. | Active map identity/dimensions and expected field IDs, polygon points, and `field.farmland.id` are present. | The exporter must be adapted to that map/runtime API or the map remains unavailable. |
| Farmland layer and ownership | Compare the probe's farmland count/IDs with the in-game farmland layer and the authenticated snapshot; request a known parcel overlay only when trusted parcel geometry exists. | IDs and ownership agree; no field polygon is presented as a farmland boundary. | The density-map source or ownership snapshot is incomplete/stale; do not claim parcel geometry. |
| PDA orientation/alignment | Render Field 22 and an irregular field, then compare the attachment with the in-game PDA/overview image. | Highlights line up in position and orientation at the same map corners/interior landmarks. | Adjust the map's explicit orientation metadata or keep the output marked geometry-only. |
| Map-change refresh | Change the loaded save/map in a disposable test context, emit a new generation, and verify the central record/reload selects only the new server/save projection. | New revision is visible, older delayed data is rejected, and no prior server/map geometry leaks into the new context. | Runtime generation/map identity detection is insufficient for that change path. |
| Discord attachment/readability | Publish a contract/farmland card through the configured Jobs channel using the exact generic mod export. | The PNG is attached, readable, and the requested selection is visibly distinguishable; text fallback remains usable when unavailable. | Discord permissions, raster scaling, or map alignment require operational adjustment. |
| FS25 file lifecycle | Restart the dedicated server and Agent around the mailbox export, including one interrupted/retried event. | The event is delivered once, malformed data is quarantined, and no proprietary map path or asset is copied into the release. | Mailbox timing or runtime file lifecycle needs a targeted fix. |

These are evidence gates, not assumptions made by the local test suite. Record
the server/client build, ZIP hashes, probe output, sanitized event metadata,
rendered image, and Discord result. Never include binding contents,
credentials, connection strings, or raw player identifiers.

## Tomorrow's validation checklist

On a stopped/testable server, capture the active map identity and overview,
then validate the normalized payload without committing the asset:

1. confirm map title, map ID, dimensions, and overview reference;
2. confirm the field list, Field 22, acreage if exposed, and its farmland ID;
3. confirm separate farmland geometry and current owner from the authenticated
   snapshot;
4. render the base map and Field 22, then compare the highlight with the FS25
   PDA; repeat with multiple fields and an ownership overlay;
5. create a test Baling contract for Field 22 only after the map is registered
   in the central test process and verify the optional jobs card attachment;
6. verify the Accept button and text-only fallback after a bot restart.

No live Mongo, Discord, VM, or FS25 server was modified by this subsystem.
