import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from fs25_network_core.protocol_harness import MailboxOperationHarness


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

