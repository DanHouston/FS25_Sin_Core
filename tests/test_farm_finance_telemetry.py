"""Native finance observations remain precise, scoped, and idempotent."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from fs25_network_core.agent import PairingAgent
from fs25_network_core.farm_finance_telemetry import (
    FarmFinanceTelemetry, FarmFinanceValidationError, validate_finance_change,
)
from fs25_network_core.event_processing import CentralEventProcessor, EventValidationError


def payload(**overrides):
    values = {"farm_id": "2", "amount": "-19.75", "balance_before": "100.00",
              "balance_after": "80.25", "money_type": "purchaseSeeds",
              "game_period": "4", "game_day": "1", "game_year": "1",
              "game_time_ms": "3600000", "source_sequence": "1"}
    values.update(overrides)
    return values


class FinanceValidationTests(unittest.TestCase):
    def test_preserves_native_category_and_actual_balance_delta(self):
        row = validate_finance_change(payload())
        self.assertEqual(row["money_type"], "purchaseSeeds")
        self.assertEqual(row["amount_raw"], "-19.75")
        self.assertEqual(row["balance_after_raw"], "80.25")
        self.assertEqual(row["game_period"], 4)
        self.assertEqual(row["source_sequence"], 1)

    def test_rejects_inconsistent_or_nonfinite_amounts(self):
        for wrong in (payload(amount="-20"), payload(amount="NaN"),
                      payload(balance_after="Infinity"), payload(farm_id="0")):
            with self.subTest(wrong=wrong), self.assertRaises(FarmFinanceValidationError):
                validate_finance_change(wrong)

    def test_rejects_bad_game_time_but_allows_unavailable_time(self):
        with self.assertRaises(FarmFinanceValidationError):
            validate_finance_change(payload(game_time_ms="999999999"))
        self.assertIsNone(validate_finance_change(payload(game_time_ms=""))["game_time_ms"])


class FinanceIngestTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.telemetry = FarmFinanceTelemetry(self.database)

    def test_world_scoped_upsert_is_stable_across_retries(self):
        first = self.telemetry.ingest("server", "save", "world", "event-1", payload())
        second = self.telemetry.ingest("server", "save", "world", "event-1", payload())
        other_world = self.telemetry.ingest("server", "save", "new-world", "event-1", payload())
        self.assertEqual(first["_id"], second["_id"])
        self.assertNotEqual(first["_id"], other_world["_id"])
        query, update = self.database.db.farm_finance_changes.update_one.call_args.args
        self.assertEqual(query["_id"], other_world["_id"])
        self.assertEqual(update["$setOnInsert"]["farm_id"], 2)
        self.assertTrue(self.database.db.farm_finance_changes.update_one.call_args.kwargs["upsert"])

    def test_requires_world_identity(self):
        with self.assertRaises(FarmFinanceValidationError):
            self.telemetry.ingest("server", "save", None, "event-1", payload())

    def test_central_rejects_invalid_finance_without_writing_it(self):
        processor = CentralEventProcessor(self.database)
        processor.registry = MagicMock()
        processor.registry.authenticate.return_value = {"server_key": "server"}
        processor.registry.resolve_save.return_value = "save"
        processor.farm_lifecycle = MagicMock()
        processor.farm_lifecycle.current_world_id.return_value = "world"
        self.database.db.processed_server_events.find_one.return_value = None
        event = {"event_id": "batch-1", "event_type": "farm_finance_batch",
                 "server_key": "server", "server_credential": "credential",
                 "save_id": "1", "world_id": "world",
                 "payload": {"changes": [payload(event_id="event-1", amount="-20")]}}
        with self.assertRaises(EventValidationError):
            processor.process(event)
        self.database.db.farm_finance_changes.update_one.assert_not_called()

    def test_batch_validates_all_rows_before_writing(self):
        with self.assertRaises(FarmFinanceValidationError):
            self.telemetry.ingest_batch("server", "save", "world", "batch", {
                "changes": [payload(event_id="one"), payload(event_id="two", amount="-20")]})
        self.database.db.farm_finance_changes.update_one.assert_not_called()

    def test_batch_keeps_individual_transaction_identity(self):
        rows = self.telemetry.ingest_batch("server", "save", "world", "batch", {
            "changes": [payload(event_id="one"), payload(event_id="two",
                                amount="5", balance_before="80", balance_after="85")]})
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["_id"], rows[1]["_id"])
        self.assertEqual(rows[1]["amount"], 5)

    def test_agent_parses_bounded_finance_batch(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "batch.xml"
            path.write_text(
                '<serverEvent event_id="batch" event_type="farm_finance_batch" '
                'server_key="server" server_credential="secret" save_id="1" world_id="world">'
                '<changes><change event_id="one" farm_id="2" amount="-19.75" '
                'balance_before="100" balance_after="80.25" money_type="purchaseSeeds"/>'
                '</changes></serverEvent>', encoding="utf-8")
            parsed = PairingAgent._parse_event_file(path)
            self.assertEqual(parsed["payload"]["changes"][0]["money_type"], "purchaseSeeds")
            self.assertEqual(parsed["payload"]["world_id"], "world")


if __name__ == "__main__":
    unittest.main()
