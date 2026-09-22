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
            # Permission receipts must retain their exact Central scope and
            # revision.  This reference harness still does not emulate FS25;
            # it only keeps the Agent/XML receipt contract realistic enough to
            # exercise receipt-gated reconciliation.
            for key in ("server_id", "save_id", "revision"):
                if root.get(key) is not None:
                    receipt.set(key, root.get(key))
            receipt.set("receipt", "deterministic reference executor")
            ElementTree.ElementTree(receipt).write(
                receipt_path, encoding="utf-8", xml_declaration=True)
            applied.append(operation_id)
        return applied


class FarmlandOwnershipReferenceExecutor:
    """Deterministic mailbox reference executor for ownership receipts.

    This is intentionally *not* a GIANTS runtime substitute.  It models the
    central/Agent/XML/receipt contract with an independently mutable owner map:
    a requested assignment and the observed post-operation owner are separate
    values.  Real FS25 validation must still exercise FarmlandManager.
    """

    def __init__(self, mailbox_dir, owners=None, valid_farms=None):
        self.root = Path(mailbox_dir)
        self.commands = self.root / "permission-commands"
        self.receipts = self.root / "permission-receipts"
        self.owners = dict(owners or {})
        self.valid_farms = set(valid_farms or ())

    def consume_once(self, force_readback_owner=None):
        self.receipts.mkdir(parents=True, exist_ok=True)
        applied = []
        for path in sorted(self.commands.glob("*.xml")):
            if path.name == "manifest.xml":
                continue
            root = ElementTree.parse(path).getroot()
            if root.get("operation_type") != "assign_farmland":
                continue
            operation_id = root.get("operation_id")
            if not operation_id or (self.receipts / (operation_id + ".xml")).exists():
                continue
            try:
                farmland_id = int(root.get("farmland_id", ""))
                farm_id = int(root.get("farm_id", ""))
            except ValueError:
                farmland_id = farm_id = -1
            owner_before = self.owners.get(farmland_id, -1)
            status, reason, mutated = "rejected", "invalid farmland or target farm", False
            if farmland_id not in self.owners or farm_id not in self.valid_farms:
                pass
            elif owner_before == farm_id:
                status, reason = "already_satisfied", "already_owned_by_target_verified"
            elif owner_before != 0:
                status, reason = "rejected", "farmland is owned by another farm"
            else:
                self.owners[farmland_id] = farm_id
                status, reason, mutated = "applied", "assigned_and_verified", True
            owner_after = self.owners.get(farmland_id, -1)
            if force_readback_owner is not None:
                owner_after = int(force_readback_owner)
            receipt = ElementTree.Element("networkLocalReceipt", operation_id=operation_id,
                operation_type="assign_farmland", server_id=root.get("server_id", ""),
                save_id=root.get("save_id", ""), farmland_id=str(farmland_id), farm_id=str(farm_id),
                owner_before_farm_id=str(owner_before), owner_farm_id=str(owner_after),
                mutation_performed=str(mutated).lower(), status=status, receipt=reason)
            ElementTree.ElementTree(receipt).write(self.receipts / (operation_id + ".xml"),
                                                    encoding="utf-8", xml_declaration=True)
            applied.append(operation_id)
        return applied
