"""Bounded hourly storage aggregation, exact replay, and volume evidence."""

import unittest
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from bson.decimal128 import Decimal128
from pymongo.errors import BulkWriteError, DuplicateKeyError

from fs25_network_core.farm_operational_telemetry import FarmOperationalTelemetry


def event(sequence, *, farm=2, fill="PEA", direction="storage_in", hour=0,
          ticks=1, liters="1.00000000000000000", year=1, day=1,
          position="100.250", runtime="runtime"):
    return {
        "event_id": f"server-{runtime}-operation-{sequence}",
        "kind": direction,
        "farm_id": str(farm),
        "source_sequence": str(sequence),
        "runtime_ms": str(sequence * 1000),
        "game_period": "4",
        "game_day": str(day),
        "game_year": str(year),
        "game_time_ms": str(hour * 3600000 + 1234),
        "fill_type": fill,
        "fill_type_index": "19" if fill == "PEA" else "20",
        "liters": liters,
        "event_count": str(ticks),
        "stock_after_liters": str(sequence),
        "runtime_node": "801",
        "position_x": position,
        "position_z": "200.500",
    }


def get_path(document, dotted):
    value = document
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


class AtomicHourlyCollection:
    """Small deterministic model of the Mongo operators used by production."""

    def __init__(self):
        self.documents = {}
        self.bulk_calls = 0
        self.find_calls = 0

    def update_one(self, query, update, *, upsert):
        bucket_id = query["_id"]
        clauses = query["$or"]
        source_path, condition = next((key, value) for clause in clauses
                                      for key, value in clause.items()
                                      if "$bitsAllClear" in value)
        bit = int(condition["$bitsAllClear"])
        current = self.documents.get(bucket_id)
        seen = get_path(current or {}, source_path)
        if current is not None and seen is not None and int(seen) & bit:
            raise DuplicateKeyError("source already applied")
        if current is None:
            current = {"_id": bucket_id}
            self.documents[bucket_id] = current
            for key, value in update["$setOnInsert"].items():
                current[key] = value
        for key, value in update["$inc"].items():
            old = current.get(key, Decimal128(Decimal(0)) if key == "liters" else 0)
            if key == "liters":
                current[key] = Decimal128(old.to_decimal() + value.to_decimal())
            else:
                current[key] = old + value
        for key, value in update["$min"].items():
            if key not in current or value < current[key]:
                current[key] = value
        for key, value in update["$max"].items():
            if key not in current or value > current[key]:
                current[key] = value
        pieces = source_path.split(".")
        parent = current
        for part in pieces[:-1]:
            parent = parent.setdefault(part, {})
        parent[pieces[-1]] = int(parent.get(pieces[-1], 0)) | bit
        for key, value in update["$addToSet"].items():
            current.setdefault(key, [])
            if value not in current[key]:
                current[key].append(value)

    def find_one(self, query, projection=None):
        current = self.documents.get(query["_id"])
        if current is None:
            return None
        path, condition = next((key, value) for key, value in query.items()
                               if key != "_id")
        value = get_path(current, path)
        if value is not None and int(value) & int(condition["$bitsAllSet"]):
            return {"_id": current["_id"]}
        return None

    def find(self, query, projection=None):
        self.find_calls += 1
        ids = set(query["_id"]["$in"])
        return [document for key, document in self.documents.items() if key in ids]

    def bulk_write(self, operations, *, ordered):
        self.bulk_calls += 1
        self.last_bulk_size = len(operations)
        self.last_ordered = ordered
        errors = []
        for index, operation in enumerate(operations):
            try:
                self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
            except DuplicateKeyError:
                errors.append({"index": index, "code": 11000, "errmsg": "source already applied"})
        if errors:
            raise BulkWriteError({"writeErrors": errors, "nInserted": 0, "nMatched": 0,
                                  "nModified": 0, "nUpserted": 0, "upserted": []})


class HourlyStorageAggregationTests(unittest.TestCase):
    def setUp(self):
        self.database = type("Database", (), {})()
        self.database.db = type("Collections", (), {})()
        self.database.db.farm_storage_hourly = AtomicHourlyCollection()
        self.database.db.farm_operational_events = MagicMock()
        self.telemetry = FarmOperationalTelemetry(self.database)

    def ingest(self, rows):
        return self.telemetry.ingest_batch("server", "save", "world", {"changes": rows})

    def test_500k_ticks_coalesce_to_exact_hourly_totals_and_replay_safely(self):
        # 500,000 engine ticks are coalesced by the one-second game hook into
        # 96 durable deliveries (24h x 2 farms x 2 crops/directions).
        per_bucket = 500000 // 96
        remainder = 500000 % 96
        rows = []
        sequence = 1
        for hour in range(24):
            for farm, fill, direction, pos in (
                (2, "PEA", "storage_in", "100.250"),
                (2, "PEA", "storage_out", "110.250"),
                (3, "SPINACH", "storage_in", "120.250"),
                (3, "SPINACH", "storage_out", "130.250"),
            ):
                ticks = per_bucket + (1 if sequence <= remainder else 0)
                rows.append(event(sequence, farm=farm, fill=fill, direction=direction,
                                  hour=hour, ticks=ticks, liters=str(Decimal(ticks) / 10),
                                  position=pos))
                sequence += 1

        # Arbitrary arrival order is safe because the destination is selected
        # only by the source event's game clock, never by ingestion time.
        self.ingest(list(reversed(rows)))
        # A newly constructed service models Central restarting while Mongo
        # keeps the same hourly documents and their compact dedupe bits.
        FarmOperationalTelemetry(self.database).ingest_batch(
            "server", "save", "world", {"changes": rows[::2]})
        self.ingest(rows)       # replay the complete retried mailbox batch

        buckets = list(self.database.db.farm_storage_hourly.documents.values())
        self.assertEqual(len(buckets), 96)
        self.assertEqual(sum(row["event_count"] for row in buckets), 500000)
        self.assertEqual(sum(row["delivery_count"] for row in buckets), 96)
        self.assertEqual(sum(row["liters"].to_decimal() for row in buckets), Decimal(50000))
        self.assertEqual({row["kind"] for row in buckets}, {"storage_in", "storage_out"})
        self.assertEqual({row["farm_id"] for row in buckets}, {2, 3})
        self.assertEqual({row["fill_type"] for row in buckets}, {"PEA", "SPINACH"})
        self.assertEqual({row["game_hour"] for row in buckets}, set(range(24)))
        self.assertTrue(all("source_event_ids" not in row for row in buckets))

    def test_partial_durable_batch_retry_is_atomic_per_movement(self):
        collection = self.database.db.farm_storage_hourly
        original = collection.update_one
        attempts = 0

        def interrupted(query, update, *, upsert):
            nonlocal attempts
            attempts += 1
            original(query, update, upsert=upsert)
            if attempts == 38:
                raise RuntimeError("simulated Central crash after durable write")

        collection.update_one = interrupted
        rows = [event(i, liters="0.125", ticks=2) for i in range(1, 129)]
        with self.assertRaises(RuntimeError):
            self.ingest(rows)
        collection.update_one = original
        FarmOperationalTelemetry(self.database).ingest_batch(
            "server", "save", "world", {"changes": rows})
        bucket = next(iter(collection.documents.values()))
        self.assertEqual(bucket["event_count"], 256)
        self.assertEqual(bucket["delivery_count"], 128)
        self.assertEqual(bucket["liters"].to_decimal(), Decimal("16.000"))

    def test_storage_batch_uses_one_prefetch_and_one_bulk_write(self):
        rows = [event(i, hour=i % 3, liters="0.125") for i in range(1, 129)]
        self.ingest(rows)
        collection = self.database.db.farm_storage_hourly
        self.assertEqual(collection.find_calls, 1)
        self.assertEqual(collection.bulk_calls, 1)
        self.assertEqual(collection.last_bulk_size, 128)
        self.assertFalse(collection.last_ordered)

    def test_replay_after_bulk_duplicate_confirms_source_bits_without_double_count(self):
        rows = [event(i, liters="0.125") for i in range(1, 4)]
        self.ingest(rows)
        self.ingest(rows)
        bucket = next(iter(self.database.db.farm_storage_hourly.documents.values()))
        self.assertEqual(bucket["delivery_count"], 3)
        self.assertEqual(bucket["liters"].to_decimal(), Decimal("0.375"))

    def test_new_runtime_can_add_to_existing_hour_bucket(self):
        self.ingest([event(1, liters="2.5")])
        FarmOperationalTelemetry(self.database).ingest_batch(
            "server", "save", "world", {"changes": [event(1, liters="3.5", runtime="restart-2")]})
        bucket = next(iter(self.database.db.farm_storage_hourly.documents.values()))
        self.assertEqual(bucket["delivery_count"], 2)
        self.assertEqual(bucket["liters"].to_decimal(), Decimal("6.0"))
        self.assertEqual(len(bucket["runtime_keys"]), 2)

    def test_distinct_sites_share_the_same_hour_bucket_and_keep_source_ids(self):
        self.ingest([event(1, liters="2.5", position="100.250")])
        self.ingest([event(2, liters="3.5", position="900.750")])

        buckets = list(self.database.db.farm_storage_hourly.documents.values())
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["liters"].to_decimal(), Decimal("6.0"))
        self.assertEqual(set(buckets[0]["source_keys"]), {"100.250:200.500", "900.750:200.500"})

    def test_retry_from_old_source_specific_bucket_is_not_counted_again(self):
        row = event(1, liters="2.5", position="100.250")
        source_field, source_mask = FarmOperationalTelemetry._storage_source_marker(
            row["event_id"], {
                "source_sequence": 1,
                "position_x": 100.25,
                "position_z": 200.5,
            })
        legacy_dimensions = ("server", "save", "world", "2", "1", "4", "1", "0",
                             "PEA", "storage_in", "100.250:200.500")
        legacy_id = hashlib.sha256("|".join(legacy_dimensions).encode("utf-8")).hexdigest()
        source_parent = self.database.db.farm_storage_hourly.documents.setdefault(
            legacy_id, {"_id": legacy_id, "liters": Decimal128(Decimal("2.5")),
                        "event_count": 1, "delivery_count": 1})
        current = source_parent
        for part in source_field.split(".")[:-1]:
            current = current.setdefault(part, {})
        current[source_field.split(".")[-1]] = source_mask

        result = self.ingest([row])

        self.assertTrue(result[0]["duplicate"])
        self.assertEqual(len(self.database.db.farm_storage_hourly.documents), 1)
        self.assertEqual(source_parent["liters"].to_decimal(), Decimal("2.5"))

    def test_missing_game_time_is_preserved_as_raw_reconcilable_event(self):
        for invalid in ("", "-1", "86400000", "bad"):
            with self.subTest(invalid=invalid):
                self.database.db.farm_operational_events.update_one.reset_mock()
                row = event(1, hour=0)
                row["game_time_ms"] = invalid
                result = self.ingest([row])
                self.assertNotIn("bucket", result[0])
                self.database.db.farm_operational_events.update_one.assert_called_once()
                self.assertEqual(len(self.database.db.farm_storage_hourly.documents), 0)


if __name__ == "__main__":
    unittest.main()
