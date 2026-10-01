# SiN FS25 Contracts

`SiN_FS25_Contracts` is a standalone diagnostic and guarded replenishment layer
for the native FS25 contract system. It does not register mission types,
construct missions, alter rewards, start or cancel work, issue payment, or copy
mission authority into SiN. FS25 remains the source of truth.

## Native runtime surface

The mod enumerates `g_missionManager:getMissions()`. The native manager assigns
each mission a stable-in-runtime `mission:getUniqueId()` and exposes its
registered type through `mission.type.name`. The list includes the mission
types the active map/game has registered; it is intentionally not hard-coded.

For field missions, the native `AbstractFieldMission` surface provides the
field object, field ID/name, area in hectares, indicator/world position,
location text, and the native reward. A field's farmland object is read when
available. A mission's `farmId` is the authoritative accepting farm after
`MissionManager:startMission` succeeds.

FS25 does not retain an accepting human player on the mission object or pass a
player identity through the manager's `startMission(mission, farmId, ...)`
contract. The diagnostic therefore reports `not-exposed-by-native-mission` for
accepting player unless a map/game version explicitly supplies a native field.
It never guesses from a connected player.

Offered lease equipment is read opportunistically from the mission's native
vehicle/group fields. Depending on type and lifecycle, FS25 exposes only a
group identifier until acceptance, or exposes instantiated vehicles after
acceptance. Each observed item may provide a configuration name, working width,
working speed and capacity. Missing fields remain unavailable.

Lifecycle observation uses these native boundaries:

| Native boundary | Diagnostic event |
| --- | --- |
| `MissionManager:registerMission` | `generated` |
| successful `MissionManager:startMission` | `accepted` |
| `AbstractMission:finish` | `finished` with native finish state |
| `MissionManager:cancelMission` | `cancelled` |
| `AbstractMission:dismiss` / `MissionManager:dismissMission` | `payment_or_dismissed` |
| periodic `MissionManager:update` scan | status/progress observation |

The `MissionManager` hooks explicitly forward every native return value. This
is required because the FS25 UI consumes `MissionStartState` and cancellation
booleans; an observer wrapper that returns `nil` can show a false “could not
start” message even while the native mission has entered `PREPARING` or
`RUNNING`.

There is no separately documented payment callback on the generic mission
surface. Native payment occurs as part of dismissal; that boundary is reported
as `payment_or_dismissed`, while the native finish state remains authoritative.
The manager's unique ID is runtime-stable, not a cross-save SiN identity.

## Estimate

For a field mission with area and usable offered equipment metrics, the mod
reports:

```text
estimated hours = area hectares * 10
                  / (working width metres * working speed km/h * 0.70)
estimated native $/hour = native reward / estimated hours
```

`0.70` is an explicit efficiency assumption covering turns, overlap,
headlands, refills and other losses. It is an estimate, not a native FS25
completion prediction. The diagnostic chooses the widest offered item that has
both a working width and that same item's explicit work speed; it never mixes a
tractor/trailer speed with another implement's width. It reports no estimate
when no paired width/speed exists. Actual duration is measured from accepted observation to
native finish observation. Transport and other non-field missions remain
observable but are normally not time-estimable without a native route metric.

The native contract details panel is extended, when the same metrics are
available, with `SiN estimated work time` and `SiN estimated native $/hour`.
The wrapper preserves every native return value and appends no rows when the
area, width, speed or reward is unavailable. Before acceptance the mod asks
the documented `MissionManager:getVehicleGroupFromIdentifier(missionType,
fieldSize, identifier)` API for the offered descriptors, including native
offers that expose only the numeric `vehicleGroup` identifier and no size.
If the pre-acceptance proxy omits `getVehicleSize`, the mod mirrors
`AbstractFieldMission`'s documented small/medium/large area thresholds without
changing mission state. When a descriptor has
only a filename/configuration (the normal native shape), the mod resolves the
matching read-only store item and loads its explicit `storeData.specs` values
(`workingWidth`, `workingWidthConfig`, and `speedLimit`) through
`StoreItemUtil.loadSpecsFromXML`. Configuration-specific width is used only
when the exact native configuration id is present. No generic bounding-box
width is used and no equipment is created by the UI hook. This makes the
estimate available on the **New** contract card for supported store metadata;
if a modded item exposes neither explicit store specs nor live vehicle metrics,
the rows remain unavailable until FS25 instantiates the lease equipment.

FS25's `Vehicle.loadSpecValueWorkingWidth` returns `{width, minWidth}`;
configuration entries also contain a `width` member. Both are decoded before
numeric validation (older builds mistakenly treated these tables as numbers).
Reference: [Vehicle](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=91&class=888&version=script).

After acceptance, when FS25 has instantiated leased equipment, the observer
reads the current implement work width from the native WorkArea specialization:
`getAIWorkAreaWidth()`, falling back to
`spec_workArea.workAreas[].workWidth`. This is the field-working width rather
than a cosmetic/configuration width. Width is unavailable for an unaccepted
offer only when its store metadata lacks an explicit working width, and remains
unavailable when the native vehicle exposes no positive work area; the observer
does not infer a width or fabricate an estimate.

## Native offer replenishment

The server-side observer starts a bounded batch of up to three native
`MissionManager` generation cycles when fewer than three `CREATED` offers are
available. The same batch policy refills any board below nine offers. Because
native generation is asynchronous, requests within a batch are normally spaced
by ten seconds; the emergency three-cycle batch below three offers is spaced by
one second. The retry window starts from the logical batch start, preventing a
burst of calls before FS25 has registered the previous offer. A
batch stops as soon as nine offers are visible, or earlier on FS25's mission
cap/native generation failure. Batch-start diagnostics are bounded to one line
per minute. The mod never constructs, registers, rewards,
or persists a custom mission. FS25 remains authoritative for
`tryGenerateMission`, field validation, vehicle groups, reward calculation,
mission caps, and save state.

Mission generation and lifecycle observer wrappers are installed only on the
authoritative server. Multiplayer clients retain only the read-only contract
details presentation hook; they never wrap native `MissionManager:update` or
`MissionManager:startMission`, preserving the vanilla Borrow Items/Accept
input path.

For every due refill, the native gate is checked before `MissionManager:update`.
When the only blocking condition is the native cooldown, the mod temporarily
expires that timer and starts the native generation cycle before entering the
native update. FS25 then performs its normal validation and registration while
the cycle is in flight; the timer is restored if the native call rejects. The
mod never generates or registers missions itself, and never requests a cycle
after native update/validation has completed. An in-flight cycle, total mission
cap, or another native rejection still blocks the request.
Three cycles are attempts, not three guaranteed offers: FS25 can find no eligible
work. Below nine, bounded batches continue; at nine, no request is made. While
fewer than three offers remain, the three cycle attempts are spaced one second
apart; refilling from three through eight remains deliberately paced at ten
seconds per cycle.

If a native generation cycle finishes without registering an offer, the
authoritative server logs a bounded `native generation completed without offer`
diagnostic with the current period and the count of exhausted cycles. This
means FS25 found no eligible native mission for the current save/month; it does
not mean a client failed to receive an existing offer.

### Controlled native field-work recovery

When fewer than three offers remain **and three consecutive native generation
cycles exhaust without an offer**, the authoritative server may prepare up to
three NPC fields for ordinary native field work. This is a supply recovery
layer, not a custom mission system: it queues the same native
`FieldState:createFieldUpdateTask()` / `g_fieldManager:addFieldUpdateTask()`
path FS25 uses for its own field updates, then waits for the normal native
`MissionManager` cycle to select, validate, register, equip, price and pay a
contract.

The rotating recovery mix is:

- herbicide/weeding: a growing, non-mature, weed-capable NPC crop receives a
  valid weed state;
- fertilizing/spraying: a growing, non-mature NPC crop has its fertilizer
  layer reduced by one native level so fertilizing work is possible;
- stone picking: a fallow NPC field receives a low stone level;
- cultivating: a fallow NPC field is put into native stubble ground state;
- plowing: a fallow NPC field has its plow counter reset.

Only the target layer changes. Each queued task starts from the field's current
native `FieldState`, retaining fruit type, growth state, lime and every other
unrelated layer. The layer is never applied to player-owned fields, fields with
an offered/active native mission, fields with a pending native update,
mission-disabled fields, mature crops, or a field recovered during the previous
hour. Recovery is capped at three fields per pass and at one pass per minute.
If FS25 rejects the resulting field state, no offer is forced into the board.

The server writes `native supply prepared` records with source layers, followed
by a bounded recovery summary. `sinContractSupply` is a read-only dedicated
server console command that reports the currently safe candidate count by work
type and exclusion reason; it never queues an update task.

For a disposable single-player or dedicated-server test save,
`sinContractSupplyTest <herbicide|fertilize|stonePick|cultivate|plow>` queues
exactly one safe NPC field update for the named work type without waiting for a
shortage. It uses the same candidate checks and update path as automatic
recovery, honors the per-field cooldown, and never creates a mission directly.
After FS25 applies the update, normal native generation must still choose and
validate the resulting offer.

`sinContractSupplyTest recovery` is the full-pass simulation: it invokes the
same rotating, maximum-three-field automatic recovery routine without waiting
for the low-offer/three-empty-cycle gate. It is useful for validating automatic
selection and queueing in a disposable save with a full contract board; it does
not make every field ineligible and does not force a tenth offer above the
nine-offer target.

## Live diagnostics

The server installs a bounded `sinContracts` console command. It logs a summary
for every current native mission, including ID, type, status, field/area,
reward, accepting farm, available lease equipment, estimate and measured actual
duration. The same records are updated when a lifecycle transition is observed.
Repeated unchanged polling is suppressed; the in-memory report is bounded to
128 missions. No native mission field is mutated.

Completion is probed only after FS25 has initialized the field mission's
density-map `completionModifier` and non-empty `completionPartitions`. The
native `getCompletion()` method is lifecycle-sensitive rather than a passive
property read; offered missions and failed starts do not have those structures
yet. Such records intentionally report `completion=unavailable` until the
native structure is ready, avoiding a diagnostic read from turning a failed
native start into a repeated `AbstractFieldMission` update error.

Use the command while the save is running, then accept a field contract, work
it normally, and run it again after success/cancellation. Compare the logged
`estimateHours`/`nativeDollarsPerHour` with `actualHours`. If a mission type
does not expose equipment metrics, the log explicitly says
`estimateHours=unavailable`.

The official GIANTS references for this surface are
[MissionManager](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=59&class=561&version=script)
and
[AbstractFieldMission](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=35&class=403&version=script).

## Deployment and limitations

Build `SiN_FS25_Contracts.zip` and install identical bytes on the dedicated
server and validation clients, alongside the existing SiN server mod. Reload
the map/save after updating because this is a script mod. It is independent of
`SiN_FS25_Server`; no mailbox, Central, MongoDB, Discord, or #sin-jobs state is
created or changed. Future work may add a read-only bridge after these native
observations are validated, but this slice intentionally does not redesign
SiN jobs or native contract economics.
