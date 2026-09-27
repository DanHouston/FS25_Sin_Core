# SiN FS25 Contracts

`SiN_FS25_Contracts` is a standalone, read-only diagnostic layer for the
native FS25 contract system. It does not register mission types, generate
missions, alter rewards, start or cancel work, issue payment, or copy mission
authority into SiN. FS25 remains the source of truth.

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
estimated hours = area hectares * 3.6
                  / (working width metres * working speed km/h * 0.70)
estimated native $/hour = native reward / estimated hours
```

`0.70` is an explicit efficiency assumption covering turns, overlap,
headlands, refills and other losses. It is an estimate, not a native FS25
completion prediction. The diagnostic chooses the widest offered implement and
the slowest positive offered speed. It reports no estimate when area, width or
speed is absent. Actual duration is measured from accepted observation to
native finish observation. Transport and other non-field missions remain
observable but are normally not time-estimable without a native route metric.

After acceptance, when FS25 has instantiated leased equipment, the observer
reads the current implement work width from the native WorkArea specialization:
`getAIWorkAreaWidth()`, falling back to
`spec_workArea.workAreas[].workWidth`. This is the field-working width rather
than a cosmetic/configuration width. Width is therefore normally unavailable
for an unaccepted offer, and remains unavailable when the native vehicle
exposes no positive work area; the observer does not infer a width or fabricate
an estimate.

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
