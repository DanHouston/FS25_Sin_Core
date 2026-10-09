"""Physical-flow and AI observations are typed, scoped, and idempotent."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from fs25_network_core.agent import PairingAgent
from fs25_network_core.event_processing import CentralEventProcessor, EventValidationError
from fs25_network_core.farm_operational_telemetry import (
    FarmOperationalTelemetry, FarmOperationalValidationError, validate_farm_operation,
)


def row(kind="sale", **overrides):
    base = {"event_id": "one", "kind": kind, "farm_id": "2", "source_sequence": "1",
            "runtime_ms": "7000", "game_period": "4", "game_day": "1",
            "game_year": "1", "game_time_ms": "3600000"}
    if kind == "sale":
        base.update(fill_type="PEA", fill_type_index="19", liters="3000.25",
                    station_price="1250.50", station_name="Grain Silo", placeable_id="p-1")
    elif kind.startswith("storage_"):
        base.update(fill_type="PEA", fill_type_index="19", liters="3000.25",
                    stock_after_liters="5000.25", runtime_node="800",
                    position_x="100.250", position_z="200.500")
    elif kind == "vehicle_usage":
        base.update(vehicle_id="vehicle-1", operating_ms="120000",
                    operating_after_ms="530000", distance_estimated_m="518.75")
    else:
        base.update(job_id="34", vehicle_id="vehicle-1")
        if kind == "ai_started":
            base["job_type"] = "AIJobFieldWork"
        else:
            base["duration_ms"] = "1800000"
            base["stop_reason"] = "completed"
    base.update(overrides)
    return base


class OperationalValidationTests(unittest.TestCase):
    def test_sale_liters_and_native_station_price_are_separate(self):
        value = validate_farm_operation(row())
        self.assertEqual(value["liters"], 3000.25)
        self.assertEqual(value["station_price"], 1250.50)
        self.assertEqual(value["fill_type"], "PEA")

    def test_storage_direction_and_end_balance(self):
        for kind in ("storage_in", "storage_out"):
            value = validate_farm_operation(row(kind))
            self.assertEqual(value["kind"], kind)
            self.assertEqual(value["stock_after_liters"], 5000.25)

    def test_ai_job_duration(self):
        self.assertEqual(validate_farm_operation(row("ai_stopped"))["duration_ms"], 1800000)

    def test_vehicle_usage_preserves_native_hours_and_estimated_distance(self):
        value = validate_farm_operation(row("vehicle_usage"))
        self.assertEqual(value["operating_ms"], 120000)
        self.assertEqual(value["distance_estimated_m"], 518.75)

    def test_rejects_malformed_and_nonfinite_physical_values(self):
        for bad in (row(liters="0"), row(liters="NaN"), row(farm_id="0"),
                    row(liters="1e999"), row(kind="not_a_kind"),
                    row("ai_stopped", duration_ms="-1")):
            with self.subTest(bad=bad), self.assertRaises(FarmOperationalValidationError):
                validate_farm_operation(bad)


class OperationalIngestTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.telemetry = FarmOperationalTelemetry(self.database)

    def test_world_scoped_upserts_are_stable_across_retries(self):
        first = self.telemetry.ingest_batch("server", "save", "world", {"changes": [row()]})[0]
        second = self.telemetry.ingest_batch("server", "save", "world", {"changes": [row()]})[0]
        new_world = self.telemetry.ingest_batch("server", "save", "other", {"changes": [row()]})[0]
        self.assertEqual(first["_id"], second["_id"])
        self.assertNotEqual(first["_id"], new_world["_id"])
        self.assertTrue(self.database.db.farm_operational_events.update_one.call_args.kwargs["upsert"])

    def test_batch_rejected_before_any_write(self):
        with self.assertRaises(FarmOperationalValidationError):
            self.telemetry.ingest_batch("server", "save", "world", {
                "changes": [row(), row("storage_in", event_id="two", liters="-5")]})
        self.database.db.farm_operational_events.update_one.assert_not_called()

    def test_partial_batch_failure_replay_preserves_exact_totals(self):
        persisted = {}
        attempts = 0

        def upsert(query, update, *, upsert):
            nonlocal attempts
            attempts += 1
            if attempts == 38:
                raise RuntimeError("simulated database interruption")
            persisted.setdefault(query["_id"], update["$setOnInsert"])

        self.database.db.farm_operational_events.update_one.side_effect = upsert
        changes = [row("storage_in", event_id=f"tick-{i}", source_sequence=str(i),
                       liters="0.25", game_time_ms="") for i in range(1, 129)]
        with self.assertRaises(RuntimeError):
            self.telemetry.ingest_batch("server", "save", "world", {"changes": changes})
        self.assertEqual(len(persisted), 37)
        self.telemetry.ingest_batch("server", "save", "world", {"changes": changes})
        self.telemetry.ingest_batch("server", "save", "world", {"changes": changes})
        self.assertEqual(len(persisted), 128)
        self.assertEqual(sum(item["liters"] for item in persisted.values()), 32)

    def test_agent_parses_nested_operations(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "operations.xml"
            path.write_text(
                '<serverEvent event_id="batch" event_type="farm_operations_batch" '
                'server_key="server" server_credential="secret" save_id="1" world_id="world">'
                '<changes><change event_id="one" kind="sale" farm_id="2" '
                'source_sequence="1" fill_type="PEA" fill_type_index="19" '
                'liters="3000.25" station_price="1250.50"/>'
                '</changes></serverEvent>', encoding="utf-8")
            event = PairingAgent._parse_event_file(path)
            self.assertEqual(event["payload"]["changes"][0]["fill_type"], "PEA")
            self.assertEqual(event["payload"]["world_id"], "world")

    def test_central_rejects_invalid_batch_without_write(self):
        processor = CentralEventProcessor(self.database)
        processor.registry = MagicMock()
        processor.registry.authenticate.return_value = {"server_key": "server"}
        processor.registry.resolve_save.return_value = "save"
        processor.farm_lifecycle = MagicMock()
        processor.farm_lifecycle.current_world_id.return_value = "world"
        self.database.db.processed_server_events.find_one.return_value = None
        event = {"event_id": "batch", "event_type": "farm_operations_batch",
                 "server_key": "server", "server_credential": "credential",
                 "save_id": "1", "world_id": "world",
                 "payload": {"changes": [row(liters="-1")]}}
        with self.assertRaises(EventValidationError):
            processor.process(event)
        self.database.db.farm_operational_events.update_one.assert_not_called()

    def test_central_accepts_valid_batch_and_uses_row_idempotency(self):
        processor = CentralEventProcessor(self.database)
        processor.registry = MagicMock()
        processor.registry.authenticate.return_value = {"server_key": "server"}
        processor.registry.resolve_save.return_value = "save"
        processor.farm_lifecycle = MagicMock()
        processor.farm_lifecycle.current_world_id.return_value = "world"
        self.database.db.processed_server_events.find_one.return_value = None
        event = {"event_id": "batch", "event_type": "farm_operations_batch",
                 "server_key": "server", "server_credential": "credential",
                 "save_id": "1", "world_id": "world", "payload": {"changes": [row()]}}
        result = processor.process(event)
        self.assertEqual(result["operation_count"], 1)
        self.database.db.farm_operational_events.update_one.assert_called_once()
        self.database.db.processed_server_events.find_one.assert_not_called()
        self.database.db.processed_server_events.insert_one.assert_not_called()

    def test_central_operation_batch_uses_row_dedupe_without_batch_marker(self):
        processor = CentralEventProcessor(self.database)
        processor.registry = MagicMock()
        processor.registry.authenticate.return_value = {"server_key": "server"}
        processor.registry.resolve_save.return_value = "save"
        processor.farm_lifecycle = MagicMock()
        processor.farm_lifecycle.current_world_id.return_value = "world"
        operation = row("storage_in", event_id="server-runtime-operation-1")
        event = {"event_id": "batch", "event_type": "farm_operations_batch",
                 "server_key": "server", "server_credential": "credential",
                 "save_id": "1", "world_id": "world", "payload": {"changes": [operation]}}
        result = processor.process(event)
        self.assertEqual(result["operation_count"], 1)
        self.database.db.processed_server_events.find_one.assert_not_called()
        self.database.db.processed_server_events.insert_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
