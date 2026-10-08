"""Authoritative, farm-owned FS25 vehicle inventory observations.

The live server snapshot is the source of truth while a machine is in-world.
These rows are not a garage archive and must never authorize a game mutation
without a fresh read-back of the exact native vehicle and its owning farm.
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
import math

from pymongo.errors import DuplicateKeyError

from .admin_manager import AdminManager
from .world_generation import WorldGenerationRegistry


MAX_VEHICLES = 5000
MAX_SNAPSHOT_AGE_SECONDS = 120
MIN_VEHICLE_CODE_LENGTH = 5
MAX_VEHICLE_CODE_LENGTH = 12


class VehicleCodeRegistry:
    """Never-reused, globally unique command codes backed by Mongo indexes.

    The native vehicle ID stays authoritative. A code belongs to one vehicle
    identity in a server/save/world, not to its current farm, so an in-world
    farm-to-farm transfer does not rename it. Records are retained after sale.
    """

    def __init__(self, database):
        self.collection = database.db.vehicle_codes

    @staticmethod
    def _scope(server_key, save_key, world_id, unique_id):
        values = (server_key, save_key, world_id, unique_id)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("complete vehicle identity scope is required")
        return dict(server_key=server_key, save_key=save_key,
                    world_id=world_id, native_unique_id=unique_id)

    @staticmethod
    def _candidate(scope, length):
        identity = [scope[key] for key in ("server_key", "save_key", "world_id", "native_unique_id")]
        seed = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(seed).digest()
        return base64.b32encode(digest).decode("ascii")[:length]

    @staticmethod
    def _existing_code(record, scope):
        if any(record.get(key) != value for key, value in scope.items()):
            raise ValueError("vehicle code registry identity mismatch")
        code = record.get("code")
        if (not isinstance(code, str) or not MIN_VEHICLE_CODE_LENGTH <= len(code) <= MAX_VEHICLE_CODE_LENGTH
                or not code.isascii() or not code.isalnum() or code != code.upper()):
            raise ValueError("vehicle code registry entry is invalid")
        return code

    def get_or_create(self, server_key, save_key, world_id, unique_id):
        scope = self._scope(server_key, save_key, world_id, unique_id)
        record = self.collection.find_one(scope)
        if record is not None:
            return self._existing_code(record, scope)
        for length in range(MIN_VEHICLE_CODE_LENGTH, MAX_VEHICLE_CODE_LENGTH + 1):
            code = self._candidate(scope, length)
            try:
                self.collection.insert_one({**scope, "code": code,
                                            "created_at": datetime.now(timezone.utc)})
            except DuplicateKeyError:
                # Another process may have registered this vehicle, or this
                # candidate belongs to a different one. Unique indexes on
                # both scope and code make either case safe under concurrency.
                record = self.collection.find_one(scope)
                if record is not None:
                    return self._existing_code(record, scope)
                continue
            return code
        raise ValueError("no unique short vehicle code is available")

    def resolve_current(self, server_key, save_key, world_id, farm_rows, code):
        """Exact-code lookup followed by fresh native-ID/farm verification."""
        if (not isinstance(code, str) or not MIN_VEHICLE_CODE_LENGTH <= len(code.strip())
                <= MAX_VEHICLE_CODE_LENGTH or not code.strip().isascii()
                or not code.strip().isalnum()):
            raise ValueError("vehicle code is invalid")
        record = self.collection.find_one({"code": code.strip().upper()})
        if (record is None or record.get("server_key") != server_key
                or record.get("save_key") != save_key or record.get("world_id") != world_id):
            raise ValueError("vehicle code is not assigned in this game world")
        matches = [row for row in farm_rows if row["unique_id"] == record.get("native_unique_id")]
        if len(matches) != 1:
            raise ValueError("vehicle is no longer in this farm's live inventory")
        return matches[0]


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
        self.codes = VehicleCodeRegistry(database)

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
        vehicles = sorted((dict(row) for row in rows if row["farm_id"] == farm_id),
                          key=lambda row: row["unique_id"])
        for vehicle in vehicles:
            vehicle["code"] = self.codes.get_or_create(
                server_key, save_key, active_world, vehicle["unique_id"])
        return {"server_key": server_key, "save_key": save_key,
                "world_id": active_world, "farm_id": farm_id,
                "farm_name": farms.get(str(farm_id), farms.get(farm_id)),
                "observed_at": received,
                "vehicles": vehicles}
