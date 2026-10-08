"""World-scoped, immutable physical-flow and AI observations from the FS25 server."""

import hashlib
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class FarmOperationalValidationError(ValueError):
    pass


def _integer(row, key, low, high, required=True):
    value = row.get(key)
    if value in (None, "") and not required:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise FarmOperationalValidationError(f"{key} must be an integer") from None
    if str(parsed) != str(value) or not low <= parsed <= high:
        raise FarmOperationalValidationError(f"{key} is invalid")
    return parsed


def _number(row, key, required=True, minimum=None):
    value = row.get(key)
    if value in (None, "") and not required:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise FarmOperationalValidationError(f"{key} must be numeric") from None
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise FarmOperationalValidationError(f"{key} is invalid")
    converted = float(parsed)
    if not math.isfinite(converted):
        raise FarmOperationalValidationError(f"{key} is invalid")
    return converted


def _label(row, key, limit, required=True):
    value = str(row.get(key) or "").strip()
    if (required and not value) or len(value) > limit:
        raise FarmOperationalValidationError(f"{key} is invalid")
    return value


def validate_farm_operation(row):
    if not isinstance(row, dict):
        raise FarmOperationalValidationError("operation must be an object")
    kind = _label(row, "kind", 32)
    if kind not in {"sale", "storage_in", "storage_out", "ai_started", "ai_stopped",
                    "vehicle_usage"}:
        raise FarmOperationalValidationError("unknown operation kind")
    value = {
        "kind": kind,
        "farm_id": _integer(row, "farm_id", 1, 254),
        "source_sequence": _integer(row, "source_sequence", 1, 2147483647),
        "runtime_ms": _integer(row, "runtime_ms", 0, 9007199254740991, required=False),
        "game_period": _integer(row, "game_period", 0, 12, required=False),
        "game_day": _integer(row, "game_day", 0, 31, required=False),
        "game_year": _integer(row, "game_year", 0, 10000, required=False),
        "game_time_ms": _integer(row, "game_time_ms", 0, 86400000, required=False),
    }
    if kind in {"sale", "storage_in", "storage_out"}:
        value.update({
            "fill_type": _label(row, "fill_type", 128),
            "fill_type_index": _integer(row, "fill_type_index", 1, 65535),
            "liters": _number(row, "liters", minimum=Decimal("0.000000001")),
        })
    if kind == "sale":
        value.update({
            "station_price": _number(row, "station_price"),
            "station_name": _label(row, "station_name", 256, required=False),
            "placeable_id": _label(row, "placeable_id", 256, required=False),
        })
    elif kind in {"storage_in", "storage_out"}:
        value.update({
            "stock_after_liters": _number(row, "stock_after_liters", minimum=Decimal(0)),
            "runtime_node": _label(row, "runtime_node", 64, required=False),
            "position_x": _number(row, "position_x", required=False),
            "position_z": _number(row, "position_z", required=False),
        })
    elif kind in {"ai_started", "ai_stopped"}:
        value.update({
            "job_id": _label(row, "job_id", 128),
            "vehicle_id": _label(row, "vehicle_id", 256, required=False),
        })
        if kind == "ai_started":
            value["job_type"] = _label(row, "job_type", 128)
        else:
            value["duration_ms"] = _integer(row, "duration_ms", 0, 9007199254740991, required=False)
            value["stop_reason"] = _label(row, "stop_reason", 256, required=False)
    else:
        value.update({
            "vehicle_id": _label(row, "vehicle_id", 256),
            "operating_ms": _integer(row, "operating_ms", 0, 9007199254740991),
            "operating_after_ms": _integer(row, "operating_after_ms", 0, 9007199254740991,
                                           required=False),
            "distance_estimated_m": _number(row, "distance_estimated_m", minimum=Decimal(0)),
        })
    return value


class FarmOperationalTelemetry:
    def __init__(self, database):
        self.db = database.db

    def ingest_batch(self, server_key, save_key, world_id, payload):
        if not str(world_id or "").strip():
            raise FarmOperationalValidationError("operation batch requires a world identity")
        changes = payload.get("changes") if isinstance(payload, dict) else None
        if not isinstance(changes, list) or not 1 <= len(changes) <= 128:
            raise FarmOperationalValidationError("operation batch must contain 1-128 changes")
        if any(not isinstance(row, dict) for row in changes):
            raise FarmOperationalValidationError("operation rows must be objects")
        ids = [_label(row, "event_id", 256) for row in changes]
        if len(ids) != len(set(ids)):
            raise FarmOperationalValidationError("duplicate operation IDs")
        # Validate the entire batch before any write. Individual upserts make
        # recovery from a partial database failure idempotent.
        values = [validate_farm_operation(row) for row in changes]
        inserted = []
        for event_id, data in zip(ids, values):
            scope = "|".join((str(server_key), str(save_key), str(world_id), event_id))
            document = {
                "_id": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
                "server_key": str(server_key), "save_key": str(save_key),
                "world_id": str(world_id), "source_event_id": event_id,
                "observed_at": datetime.now(timezone.utc), **data,
            }
            self.db.farm_operational_events.update_one(
                {"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True)
            inserted.append(document)
        return inserted
