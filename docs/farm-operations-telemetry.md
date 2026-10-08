# Farm operations telemetry

`FS25_SiN_Server` passively observes four additional server-side streams:

- `sale`: physical liters and the selling station's returned price, by farm,
  commodity, and station. The station return is not guaranteed to include
  later mod bonuses; the independent finance stream records actual balance
  changes. Sales to production inputs are storage changes, not NPC sales.
- `storage_in` / `storage_out`: actual native `Storage:setFillLevel` deltas by
  farm, fill type, and storage root location, including production input/output
  storage. Opposing flows are kept separate within each one-second window.
  The runtime node is not a durable placeable ID; location is retained for
  future placeable association.
- `ai_started` / `ai_stopped`: native AI job lifecycle, job and vehicle ID, and
  elapsed real time when both endpoints are observed in the same runtime.
  Restart-spanning or pre-existing jobs may lack duration.
- `vehicle_usage`: native operating-time delta plus estimated travel distance
  for farm vehicles, sampled from server positions every ten seconds and
  emitted at minute granularity. Large teleports are excluded. The distance
  is an **approximation**, not a game odometer or exact mileage.

Game callbacks only update bounded Lua records. One `farm_operations_batch`
mailbox XML (up to 128 individual records) is attempted per second; each row
has a world-scoped ID and Central idempotently stores it in
`farm_operational_events`. A full 2,048-row buffer warns once and drops further
observations rather than blocking gameplay. Load-time storage initialization
is excluded using the native running-state flag. No new client state or
gameplay mutation is introduced. The game mod, Agent, and Central must deploy
together because older Agents reject the new event type.

These observations do not yet prove which field produced a crop, account for
all loose ground crops, identify the human driver, or reconstruct every recipe
cycle. Native finance-sheet history before deployment is also not backfilled.
Those are follow-on capture/reporting tasks, not inferred from a sale alone.

Dedicated-server validation: sell a known quantity, load and unload an owned
silo, run and stop an AI worker, then operate a vehicle for a minute. Compare
the native sale, stock, AI duration, and operating hours with Central rows;
verify duplicate mailbox delivery does not add rows. Run this once with a
remote client to confirm only the server emitted the observations.
