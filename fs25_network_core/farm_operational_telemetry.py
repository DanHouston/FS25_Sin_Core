"""World-scoped, immutable physical-flow and AI observations from the FS25 server."""

import hashlib
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bson.decimal128 import Decimal128
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError


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


def _calendar_integer(row, key, low, high):
    """Unusable game-clock fields remain raw instead of blocking the batch."""
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if str(parsed) != str(value) or not low <= parsed <= high:
        return None
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
        "game_period": _calendar_integer(row, "game_period", 1, 12),
        "game_day": _calendar_integer(row, "game_day", 1, 31),
        "game_year": _calendar_integer(row, "game_year", 1, 10000),
        "game_time_ms": _calendar_integer(row, "game_time_ms", 0, 86400000),
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
            "event_count": _integer(row, "event_count", 1, 2147483647,
                                    required=False) or 1,
            # The existing float remains for legacy raw-event consumers; the
            # Decimal128 hourly total uses the original 17-digit Lua value.
            "liters_raw": str(Decimal(str(row["liters"]))),
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
        storage_movements = []
        for event_id, data in zip(ids, values):
            scope = "|".join((str(server_key), str(save_key), str(world_id), event_id))
            if (data["kind"] in {"storage_in", "storage_out"}
                    and self._has_game_hour(data)
                    and self._storage_source_marker(event_id, data) is not None):
                storage_movements.append(self._ingest_storage_hour(
                    server_key, save_key, world_id, event_id, data))
                continue
            document = {
                "_id": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
                "server_key": str(server_key), "save_key": str(save_key),
                "world_id": str(world_id), "source_event_id": event_id,
                "observed_at": datetime.now(timezone.utc), **data,
            }
            self.db.farm_operational_events.update_one(
                {"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True)
            inserted.append(document)
        inserted.extend(self._ingest_storage_hours(storage_movements))
        return inserted

    @staticmethod
    def _has_game_hour(data):
        return (data["game_year"] is not None and data["game_year"] >= 1
                and data["game_period"] is not None and 1 <= data["game_period"] <= 12
                and data["game_day"] is not None and 1 <= data["game_day"] <= 31
                and data["game_time_ms"] is not None
                and 0 <= data["game_time_ms"] < 86400000)

    @staticmethod
    def _storage_source_marker(event_id, data):
        """Compact replay protection: one bit per source sequence per retained bucket."""
        match = re.match(r"^(.*)-operation-(\d+)$", event_id)
        if match is None:
            return None
        sequence = int(match.group(2))
        if sequence != data["source_sequence"]:
            return None
        runtime_key = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()[:16]
        zero_based = sequence - 1
        chunk, bit = divmod(zero_based, 31)
        field = f"seen.{runtime_key}.{chunk}"
        mask = 1 << bit
        return field, mask

    def _ingest_storage_hour(self, server_key, save_key, world_id, event_id, data):
        hour = data["game_time_ms"] // 3600000
        # Source position is evidence, not a bucket dimension: all sites for
        # this farm/fill/direction/game-hour roll into one durable record.
        # Keep the old source-specific key below only to recognize deliveries
        # already applied before this wider aggregation key was deployed.
        if data["position_x"] is not None and data["position_z"] is not None:
            source_key = f"{data['position_x']:.3f}:{data['position_z']:.3f}"
        else:
            source_key = "unknown"
        dimensions = (str(server_key), str(save_key), str(world_id),
                      str(data["farm_id"]), str(data["game_year"]),
                      str(data["game_period"]), str(data["game_day"]),
                      str(hour), data["fill_type"], data["kind"])
        bucket_id = hashlib.sha256("|".join(dimensions).encode("utf-8")).hexdigest()
        legacy_dimensions = dimensions + (source_key,)
        legacy_bucket_id = hashlib.sha256("|".join(legacy_dimensions).encode("utf-8")).hexdigest()
        source_field, source_mask = self._storage_source_marker(event_id, data)
        runtime_key = source_field.split(".")[1]
        received_at = datetime.now(timezone.utc)
        update = {
            "$setOnInsert": {"server_key": str(server_key), "save_key": str(save_key),
                             "world_id": str(world_id), "farm_id": data["farm_id"],
                             "game_year": data["game_year"],
                             "game_period": data["game_period"],
                             "game_day": data["game_day"], "game_hour": hour,
                             "fill_type": data["fill_type"], "kind": data["kind"]},
            "$inc": {"liters": Decimal128(Decimal(data["liters_raw"])),
                     "event_count": data["event_count"], "delivery_count": 1},
            "$min": {"first_event_time_ms": data["game_time_ms"],
                     "source_sequence_min": data["source_sequence"],
                     "first_received_at": received_at},
            "$max": {"last_event_time_ms": data["game_time_ms"],
                     "source_sequence_max": data["source_sequence"],
                     "last_received_at": received_at},
            "$bit": {source_field: {"or": source_mask}},
            "$addToSet": {"source_keys": source_key, "runtime_keys": runtime_key,
                          "runtime_nodes": data["runtime_node"] or "unknown"},
        }
        return {"_id": bucket_id, "legacy_bucket_id": legacy_bucket_id,
                "source_field": source_field, "source_mask": source_mask,
                "update": update, "data": data}

    @staticmethod
    def _marker_is_set(document, field, mask):
        value = document
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        try:
            return value is not None and int(value) & int(mask) == int(mask)
        except (TypeError, ValueError):
            return False

    def _ingest_storage_hours(self, movements):
        """Apply hourly movements with batched reads/writes and per-row atomicity."""
        if not movements:
            return []
        collection = self.db.farm_storage_hourly
        legacy_ids = list({movement["legacy_bucket_id"] for movement in movements})
        marker_projection = {"_id": 1}
        marker_projection.update({movement["source_field"]: 1 for movement in movements})
        legacy_documents = {
            document["_id"]: document
            for document in collection.find({"_id": {"$in": legacy_ids}}, marker_projection)
        }
        operations = []
        results = []
        for movement in movements:
            if self._marker_is_set(legacy_documents.get(movement["legacy_bucket_id"]),
                                   movement["source_field"], movement["source_mask"]):
                results.append({"_id": movement["legacy_bucket_id"], "bucket": "game_hour",
                                "duplicate": True, **movement["data"]})
                continue
            query = {"_id": movement["_id"], "$or": [
                {movement["source_field"]: {"$exists": False}},
                {movement["source_field"]: {"$bitsAllClear": movement["source_mask"]}},
            ]}
            operations.append(UpdateOne(query, movement["update"], upsert=True))
            results.append({"_id": movement["_id"], "bucket": "game_hour", **movement["data"]})
        if not operations:
            return results
        try:
            # Each update atomically writes its dedupe bit and aggregate delta.
            # A partial bulk failure is safe to replay; the source bit prevents
            # every successfully applied row from being counted twice.
            collection.bulk_write(operations, ordered=False)
            return results
        except BulkWriteError as error:
            details = error.details or {}
            write_errors = details.get("writeErrors", [])
            if not write_errors or any(item.get("code") != 11000 for item in write_errors):
                raise
            # An unordered upsert can report duplicate _id when a concurrent
            # replay already set that source bit. Verify all rows with one read
            # before acknowledging the envelope.
            current_ids = list({movement["_id"] for movement in movements})
            current_documents = {
                document["_id"]: document
                for document in collection.find({"_id": {"$in": current_ids}}, marker_projection)
            }
            for movement in movements:
                current = current_documents.get(movement["_id"])
                legacy = legacy_documents.get(movement["legacy_bucket_id"])
                if not (self._marker_is_set(current, movement["source_field"],
                                            movement["source_mask"])
                        or self._marker_is_set(legacy, movement["source_field"],
                                               movement["source_mask"])):
                    raise RuntimeError("storage bucket contention; retry the batch") from error
            return results
