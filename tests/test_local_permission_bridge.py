import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fs25_network_core.local_permission_bridge import LocalPermissionBridge


class LocalPermissionBridgeTests(unittest.TestCase):
    def test_delivery_does_not_acknowledge_and_success_receipt_does(self):
        auth = MagicMock()
        auth.db.permission_jobs.find.return_value = [{"_id": "op", "server_id": "local-dev", "save_id": "save", "game_player_id": "p", "farm_id": 1, "role": "farm_manager", "revision": 1, "state": "pending"}]
        with tempfile.TemporaryDirectory() as folder:
            bridge = LocalPermissionBridge(auth, folder, "local-dev", "save")
            self.assertEqual(bridge.deliver(), ["op"])
            self.assertTrue((Path(folder) / "permission-commands/op.xml").is_file())
            auth.acknowledge.assert_not_called()
            receipts = Path(folder) / "permission-receipts"; receipts.mkdir()
            (receipts / "op.xml").write_text('<permissionReceipt operation_id="op" server_id="local-dev" save_id="save" revision="1" status="applied" receipt="fs25"/>')
            self.assertEqual(bridge.consume_receipts(), ["op"])
            auth.acknowledge.assert_called_once_with(
                "op", "local-dev", "save", 1,
                {"operation_id": "op", "server_id": "local-dev", "save_id": "save", "revision": "1",
                 "status": "applied", "receipt": "fs25"}, None)

    def test_authority_snapshot_preserves_personal_manager_and_shared_contractor(self):
        auth = MagicMock()
        auth.db.permission_jobs.find.return_value = []
        auth.db.memberships.find.return_value = [{
            "game_player_id": "p", "farm_id": 2, "desired_role": "farm_manager",
            "state": "active", "applied_role": "farm_manager",
        }, {
            "game_player_id": "p", "farm_id": 99, "desired_role": "contractor",
            "source_farm_id": 2, "state": "pending", "applied_role": None,
        }]
        with tempfile.TemporaryDirectory() as folder:
            bridge = LocalPermissionBridge(auth, folder, "local-dev", "save")
            bridge.deliver()
            authority = (Path(folder) / "manager-authority.xml").read_text(encoding="utf-8")
            self.assertIn('manager gamePlayerId="p" farmId="2"', authority)
            self.assertIn('contractor gamePlayerId="p" farmId="99" sourceFarmId="2"', authority)

    def test_invalid_receipt_is_quarantined_without_acknowledgment(self):
        auth = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            receipts = Path(folder) / "permission-receipts"
            receipts.mkdir()
            invalid = receipts / "false-positive.xml"
            invalid.write_text(
                '<permissionReceipt operation_id="false-positive" server_id="local-dev" '
                'save_id="save" revision="1" status="pending_validation" receipt="not verified"/>',
                encoding="utf-8")
            bridge = LocalPermissionBridge(auth, folder, "local-dev", "save")
            self.assertEqual(bridge.consume_receipts(), [])
            auth.acknowledge.assert_not_called()
            self.assertFalse(invalid.exists())
            self.assertTrue((receipts / "false-positive.xml.failed").exists())
