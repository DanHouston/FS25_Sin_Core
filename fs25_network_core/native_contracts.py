"""Durable projection of server-authoritative native FS25 contract facts."""
import hashlib
import re
from datetime import datetime, timezone


LIFECYCLES = {"available", "accepted", "completed", "cancelled", "expired"}
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def contract_key(server_key, save_key, world_id, mission_id):
    value = "|".join((str(server_key), str(save_key), str(world_id or "legacy"), str(mission_id)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_contract_code(mission_id):
    """Return a stable SiN-facing contract reference for a native mission ID."""
    value = str(mission_id or "").strip()
    if not value:
        raise ValueError("native mission ID is required")
    if (value.isascii() and value.isdigit() and (value == "0" or not value.startswith("0"))
            and int(value) <= 9007199254740991):
        number = int(value)
        encoded = "0" if number == 0 else ""
        while number:
            number, remainder = divmod(number, 36)
            encoded = _BASE36[remainder] + encoded
        return "C" + encoded
    # FS25 native field missions use UUID-like IDs such as
    # "mission0f55fa33df33af7807912d...". A 64-bit stable prefix keeps the
    # player-facing reference manageable while the full native ID remains the
    # authoritative identity in Mongo. The unique world/code index detects the
    # extraordinarily unlikely prefix collision instead of silently aliasing.
    match = re.fullmatch(r"mission([0-9a-fA-F]{16,})", value, flags=re.IGNORECASE)
    if match:
        return "S" + match.group(1)[:16].upper()
    # Keep unfamiliar GIANTS IDs intact rather than creating a truncated alias.
    return "X" + value


def mission_id_from_contract_code(code):
    """Decode reversible public references; hashed short codes resolve via Mongo."""
    value = str(code or "").strip()
    if len(value) < 2:
        return None
    prefix, suffix = value[0].upper(), value[1:]
    if prefix == "C" and all(char.upper() in _BASE36 for char in suffix):
        number = int(suffix, 36)
        candidate = str(number)
        if native_contract_code(candidate).upper() == value.upper():
            return candidate
    if prefix == "X":
        return suffix
    return None


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
        public_code = str(payload.get("contract_code") or native_contract_code(mission_id))
        if public_code != native_contract_code(mission_id):
            raise ValueError("native contract code does not match mission identity")
        key = contract_key(server_key, save_key, world_id, mission_id)
        now = datetime.now(timezone.utc)
        fields = {
            "mission_id": mission_id,
            "contract_code": public_code,
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
        # Keep an append-only, idempotent lifecycle history beside the current
        # projection. The observation clock is Central receipt time; native
        # game-clock fields are retained separately and are never substituted.
        history_id = hashlib.sha256(("|".join((str(server_key), str(save_key),
                                               str(world_id or "legacy"), str(event_id)))).encode("utf-8")).hexdigest()
        history = {"_id": history_id, "server_key": str(server_key),
                   "save_key": str(save_key), "world_id": str(world_id or "legacy"),
                   "mission_id": mission_id, "event_id": str(event_id),
                   "contract_code": public_code,
                   "lifecycle": lifecycle, "mission_type": fields["mission_type"],
                   "outcome": ("completed" if lifecycle == "completed" else
                               "cancelled" if lifecycle == "cancelled" else
                               "expired" if lifecycle == "expired" else None),
                   "finish_state": str(payload.get("finish_state") or "") or None,
                   "field_id": fields["field_id"], "field_name": fields["field_name"],
                   "area_ha": fields["area_ha"], "accepting_farm_id": fields["accepting_farm_id"],
                   "accepting_player": fields["accepting_player"], "reward": fields["reward"],
                   "actual_payout": _number(payload, "actual_payout"),
                   "game_runtime_ms": _number(payload, "game_runtime_ms"),
                   "game_year": _number(payload, "game_year"),
                   "game_period": _number(payload, "game_period"),
                   "game_day": _number(payload, "game_day"),
                   "game_time_ms": _number(payload, "game_time_ms"),
                   "observed_at": now}
        self.db.native_contract_lifecycle_events.update_one(
            {"_id": history_id}, {"$setOnInsert": history}, upsert=True)
        fields[f"{lifecycle}_observed_at"] = now
        if history["actual_payout"] is not None:
            fields["actual_payout"] = history["actual_payout"]
        self.db.native_contracts.update_one(
            {"_id": key},
            {"$setOnInsert": {"server_key": str(server_key), "save_key": str(save_key),
                              "world_id": str(world_id or "legacy"), "created_at": now},
             "$set": fields}, upsert=True)
        return self.db.native_contracts.find_one({"_id": key}) or {"_id": key, **fields}
