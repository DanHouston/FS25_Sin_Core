import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from fs25_network_core.protocol_harness import MailboxOperationHarness, FarmlandOwnershipReferenceExecutor


class MailboxOperationHarnessTests(unittest.TestCase):
    def command(self, root, operation_id="operation-1"):
        commands = root / "permission-commands"
        commands.mkdir(parents=True, exist_ok=True)
        ElementTree.ElementTree(ElementTree.Element(
            "networkLocalCommand", operation_id=operation_id,
            operation_type="vehicle_transfer")).write(
                commands / (operation_id + ".xml"), encoding="utf-8", xml_declaration=True)

    def test_applied_command_emits_one_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.command(root)
            harness = MailboxOperationHarness(root)
            self.assertEqual(harness.consume_once(), ["operation-1"])
            receipt = ElementTree.parse(root / "permission-receipts" / "operation-1.xml").getroot()
            self.assertEqual(receipt.get("status"), "applied")

    def test_duplicate_execution_is_reported_as_already_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.command(root)
            harness = MailboxOperationHarness(root)
            harness.consume_once()
            (root / "permission-receipts" / "operation-1.xml").unlink()
            self.assertEqual(harness.consume_once(duplicate_execution=True), ["operation-1"])
            receipt = ElementTree.parse(root / "permission-receipts" / "operation-1.xml").getroot()
            self.assertEqual(receipt.get("status"), "already_applied")

    def test_lost_receipt_leaves_command_available_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.command(root)
            harness = MailboxOperationHarness(root)
            self.assertEqual(harness.consume_once(lose_receipt=True), [])
            self.assertTrue((root / "permission-commands" / "operation-1.xml").exists())
            self.assertEqual(harness.consume_once(), ["operation-1"])


class FarmlandOwnershipReferenceExecutorTests(unittest.TestCase):
    def command(self, root, operation_id="land-1", farmland_id="12", farm_id="2"):
        commands = root / "permission-commands"
        commands.mkdir(parents=True, exist_ok=True)
        ElementTree.ElementTree(ElementTree.Element("networkLocalCommand", operation_id=operation_id,
            operation_type="assign_farmland", server_id="server", save_id="save",
            farmland_id=farmland_id, farm_id=farm_id)).write(
                commands / (operation_id + ".xml"), encoding="utf-8", xml_declaration=True)

    def test_observed_owner_is_independent_of_requested_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.command(root)
            executor = FarmlandOwnershipReferenceExecutor(root, owners={12: 0}, valid_farms={2})
            self.assertEqual(executor.consume_once(force_readback_owner=9), ["land-1"])
            receipt = ElementTree.parse(root / "permission-receipts" / "land-1.xml").getroot()
            self.assertEqual(receipt.get("status"), "applied")
            self.assertEqual(receipt.get("owner_before_farm_id"), "0")
            self.assertEqual(receipt.get("owner_farm_id"), "9")
            self.assertEqual(receipt.get("mutation_performed"), "true")

    def test_foreign_owner_rejection_and_already_satisfied_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.command(root, "foreign")
            self.command(root, "satisfied", farmland_id="13")
            executor = FarmlandOwnershipReferenceExecutor(root, owners={12: 9, 13: 2}, valid_farms={2})
            self.assertEqual(executor.consume_once(), ["foreign", "satisfied"])
            foreign = ElementTree.parse(root / "permission-receipts" / "foreign.xml").getroot()
            satisfied = ElementTree.parse(root / "permission-receipts" / "satisfied.xml").getroot()
            self.assertEqual((foreign.get("status"), foreign.get("owner_farm_id")), ("rejected", "9"))
            self.assertEqual((satisfied.get("status"), satisfied.get("mutation_performed")),
                             ("already_satisfied", "false"))
