"""Authoritative, farm-owned FS25 vehicle inventory observations.

The live server snapshot is the source of truth while a machine is in-world.
These rows are not a garage archive and must never authorize a game mutation
without a fresh read-back of the exact native vehicle and its owning farm.
"""

from datetime import datetime, timezone
import math

from .admin_manager import AdminManager
from .world_generation import WorldGenerationRegistry


MAX_VEHICLES = 5000
MAX_SNAPSHOT_AGE_SECONDS = 120


def validate_vehicle_inventory(snapshot):
    """Reject incomplete or contradictory authoritative inventory rows."""
    if snapshot.get("vehicle_inventory_ready") is not True:
        if snapshot.get("vehicles"):
            raise ValueError("unready vehicle inventory cannot contain rows")
        return None
    rows = snapshot.get("vehicles")
    farms = snapshot.get("farms")
    if not isinstance(rows, list) or len(rows) > MAX_VEHICLES or not isinstance(farms, dict):
        raise ValueError("invalid vehicle inventory")
    farm_ids = {str(farm_id) for farm_id in farms}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid vehicle inventory row")
        unique_id, filename = row.get("unique_id"), row.get("filename")
        farm_id = row.get("farm_id")
        if (not isinstance(unique_id, str) or not 0 < len(unique_id) <= 160
                or unique_id in seen or not isinstance(filename, str)
                or not 0 < len(filename) <= 1024 or type(farm_id) is not int
                or not 1 <= farm_id <= 254 or str(farm_id) not in farm_ids):
            raise ValueError("invalid vehicle identity or owner")
        seen.add(unique_id)
        for field, limit in (("name", 240), ("mod_name", 160)):
            value = row.get(field, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("invalid vehicle metadata")
        if "sell_value" in row:
            value = row["sell_value"]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("invalid vehicle sell value")
    return rows


class VehicleInventoryService:
    """Read a manager's farm vehicles from the latest verified game snapshot."""

    def __init__(self, database):
        self.db = database.db
        self.admin = AdminManager(database)
        self.worlds = WorldGenerationRegistry(database)

    def for_manager(self, discord_id, server_key, save_key, world_id):
        active_world = self.worlds.require_active(server_key, save_key, world_id)
        link = self.admin.lookup(discord_id, server_key, save_key, world_id=active_world)
        farm_id = link.get("farm_id")
        if type(farm_id) is not int or not 1 <= farm_id <= 254:
            raise ValueError("verified farm owner is unavailable")
        snapshot = self.db.server_snapshots.find_one({
            "server_key": server_key, "save_key": save_key, "world_id": active_world,
            "source": "game"}, sort=[("received_at", -1)])
        if not isinstance(snapshot, dict):
            raise ValueError("no authoritative game snapshot is available")
        received = snapshot.get("received_at")
        if not isinstance(received, datetime):
            raise ValueError("game snapshot timestamp is unavailable")
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - received).total_seconds()
        if age < -30 or age > MAX_SNAPSHOT_AGE_SECONDS:
            raise ValueError("game vehicle inventory is stale")
        rows = validate_vehicle_inventory(snapshot)
        if rows is None:
            raise ValueError("game vehicle inventory is unavailable")
        farms = snapshot["farms"]
        return {"server_key": server_key, "save_key": save_key,
                "world_id": active_world, "farm_id": farm_id,
                "farm_name": farms.get(str(farm_id), farms.get(farm_id)),
                "observed_at": received,
                "vehicles": sorted((dict(row) for row in rows if row["farm_id"] == farm_id),
                                   key=lambda row: row["unique_id"])}
