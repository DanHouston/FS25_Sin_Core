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

## Native generation and controlled field-work recovery

FS25's `MissionManager` remains the sole authority for contract generation,
validation, registration, rewards, mission limits, and lifecycle. On the server,
SiN sets the native `MissionManager.MISSION_GENERATION_INTERVAL` to 10 seconds
and observes native generation start/completion. It does not override
`MissionManager:update`, call `startMissionGeneration` itself, or write
`generationTimer`. There are no SiN refill batches, retry loops, or guaranteed
offer counts; FS25 decides whether a valid mission can be generated.

For `plowMission`, `cultivateMission`, `sowMission`, `harvestMission`, and
`mowMission`, SiN may prefer a different free NPC field only when that mission
type's native `isAvailableForField` predicate accepts it. The native field
picker still runs, and native mission generation performs its normal validation.
No other mission type receives this preference.

If three consecutive native generation cycles finish without increasing the
available `CREATED` offer count, the server may queue field recovery when fewer
than nine offers remain. Nine is a soft target: SiN may prepare work that lets
FS25 generate future offers, but never creates or forces a contract to reach the
target. Recovery queues at most three ordinary `FieldState` update tasks and is
subject to the cooldown and NPC-field safety checks.

Automatic recovery is limited to:

- herbicide: give an eligible growing, non-mature, weed-capable NPC crop a
  native weed state;
- fertilize: lower an eligible growing, non-mature NPC crop's fertilizer layer
  by one native level;
- stone picking: give an eligible fallow NPC field a low stone level.

Automatic recovery never changes plow or cultivation state. Each task preserves
the field's unrelated native state and excludes owned, occupied, pending,
mission-disabled, invalid, mature, or recently recovered fields. If FS25 rejects
the resulting field state, no contract is forced onto the board.

`sinContractSupply` is a read-only server console diagnostic. The explicit
`sinContractSupplyTest` command can exercise supported recovery actions in a
disposable save; its `recovery` mode simulates the automatic maximum-three-field
pass without waiting for three empty cycles. It still queues only native field
update tasks and never creates a mission. The explicit one-field diagnostic
also accepts cultivate/plow for controlled testing; those actions are never in
the automatic recovery list and automatic recovery never changes ground/plow
state.

Generation exhaustion details, field censuses/samples, construction traces,
field-state detail, and equipment detail are DEBUG-only. Normal startup and
operation retain concise cap-adjustment, field-substitution, recovery-summary,
and mission lifecycle logs.

## Live diagnostics

The server installs a bounded `sinContracts` console command. By default it
prints a concise mission-count snapshot; per-mission field/state/equipment and
estimate details are behind the disabled-by-default DEBUG flag. The same
in-memory records are updated when lifecycle transitions are observed, are
bounded to 128 missions, and never mutate native mission fields.

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

## Discord native-contract threads

When `SiN_FS25_Server` and `SiN_FS25_Contracts` are installed together, the
Contracts mod observes native MissionManager registration, successful
acceptance, successful completion, and cancellation on the authoritative
server. It emits those facts through the server mod's authenticated event
mailbox; it does not create or modify missions. The Central service projects
each mission by server/save/world/mission ID, and JiN publishes one available
parent card to that server's configured Activity channel with a Discord thread.
Claimed, completed, and cancelled updates are posted into that contract's
thread. The accepting farm is reported when the native mission exposes it;
FS25 does not consistently expose the human player identity, so the bot does
not guess a name.

The bot needs `View Channel`, `Send Messages`, `Create Public Threads`, and
`Send Messages in Threads` in the configured Activity channel. Missing thread
permissions fail the outbox item with a diagnostic rather than silently
publishing a parent-only card. The channel is the server-specific Activity
channel configured in the server registry, not the guild-wide `#sin-jobs`
channel used by Discord-created SiN work contracts.

## Deployment and limitations

Build `SiN_FS25_Contracts.zip` and install identical bytes on the dedicated
server and validation clients, alongside the existing SiN server mod. Reload
the map/save after updating because this is a script mod. It is independent of
`SiN_FS25_Server`; native contract events use its authenticated mailbox and
Central/JiN projection described above. This does not redesign SiN jobs or
native contract economics.
