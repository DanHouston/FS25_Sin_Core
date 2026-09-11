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
            auth.acknowledge.assert_called_once_with("op", "local-dev", "save", 1, "fs25")
