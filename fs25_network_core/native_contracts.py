"""Durable projection of server-authoritative native FS25 contract facts."""
import hashlib
from datetime import datetime, timezone


LIFECYCLES = {"available", "accepted", "completed", "cancelled"}


def contract_key(server_key, save_key, world_id, mission_id):
    value = "|".join((str(server_key), str(save_key), str(world_id or "legacy"), str(mission_id)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(payload, key):
    value = payload.get(key)
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class NativeContractService:
    """Keep Discord presentation state separate from FS25 mission authority."""
    def __init__(self, database):
        self.database = database
        self.db = database.db

    def ingest(self, server_key, save_key, world_id, event_id, lifecycle, payload):
        if lifecycle not in LIFECYCLES:
            raise ValueError("unsupported native contract lifecycle")
        if not isinstance(payload, dict) or not str(payload.get("mission_id") or "").strip():
            raise ValueError("native contract event requires mission_id")
        mission_id = str(payload["mission_id"])
        key = contract_key(server_key, save_key, world_id, mission_id)
        now = datetime.now(timezone.utc)
        fields = {
            "mission_id": mission_id,
            "lifecycle": lifecycle,
            "mission_type": str(payload.get("mission_type") or "unknown"),
            "field_id": str(payload.get("field_id") or ""),
            "farmland_id": str(payload.get("farmland_id") or ""),
            "field_name": str(payload.get("field_name") or ""),
            "area_ha": _number(payload, "area_ha"),
            "reward": _number(payload, "reward"),
            "estimated_hours": _number(payload, "estimated_hours"),
            "estimated_dollars_per_hour": _number(payload, "estimated_dollars_per_hour"),
            "equipment_count": _number(payload, "equipment_count"),
            "accepting_farm_id": str(payload.get("accepting_farm_id") or ""),
            "accepting_farm_name": str(payload.get("accepting_farm_name") or ""),
            "accepting_player": str(payload.get("accepting_player") or ""),
            "last_event_id": str(event_id), "updated_at": now,
        }
        self.db.native_contracts.update_one(
            {"_id": key},
            {"$setOnInsert": {"server_key": str(server_key), "save_key": str(save_key),
                              "world_id": str(world_id or "legacy"), "created_at": now},
             "$set": fields}, upsert=True)
        return self.db.native_contracts.find_one({"_id": key}) or {"_id": key, **fields}
