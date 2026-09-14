"""Local mailbox protocol simulator for integration tests.

This deliberately simulates only the durable XML boundary.  It does not
pretend to implement Farming Simulator APIs or central business authority.
"""
from pathlib import Path
from xml.etree import ElementTree


class MailboxOperationHarness:
    """Consume Agent command XML and emit deterministic receipt XML."""

    def __init__(self, mailbox_dir):
        self.root = Path(mailbox_dir)
        self.commands = self.root / "permission-commands"
        self.receipts = self.root / "permission-receipts"
        self.executed = set()

    def consume_once(self, status="applied", lose_receipt=False, duplicate_execution=False):
        if status not in {"applied", "already_applied", "failed", "pending_validation",
                          "definitively_not_applied"}:
            raise ValueError("unsupported simulated receipt status")
        self.receipts.mkdir(parents=True, exist_ok=True)
        applied = []
        for path in sorted(self.commands.glob("*.xml")):
            if path.name == "manifest.xml":
                continue
            root = ElementTree.parse(path).getroot()
            operation_id = root.get("operation_id")
            if not operation_id:
                continue
            already_executed = operation_id in self.executed
            if not already_executed or duplicate_execution:
                self.executed.add(operation_id)
            if lose_receipt:
                continue
            receipt_path = self.receipts / (operation_id + ".xml")
            if receipt_path.exists():
                continue
            receipt = ElementTree.Element(
                "networkLocalReceipt",
                operation_id=operation_id,
                operation_type=root.get("operation_type", ""),
                status="already_applied" if already_executed else status,
            )
            ElementTree.ElementTree(receipt).write(
                receipt_path, encoding="utf-8", xml_declaration=True)
            applied.append(operation_id)
        return applied
