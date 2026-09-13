import json
import io
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from fs25_network_core import agent as agent_module
from fs25_network_core.agent import PairingAgent


class Response:
    status = 200

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps({"server_key": "oak-ridge", "credential": "secret-value", "status": "accepted"}).encode()


class AgentTests(unittest.TestCase):
    def test_pair_once_writes_response_without_requiring_request_xml(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            server_key = PairingAgent(folder, "https://central.example", opener).pair_once("abc123")
            self.assertEqual(server_key, "oak-ridge")
            self.assertTrue((Path(folder) / "permission-commands/server-pairing-response.xml").exists())

    def test_pair_cli_prints_server_key_but_no_secret(self):
        fake_agent = MagicMock()
        fake_agent.pair_once.return_value = "oak-ridge"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(agent_module, "PairingAgent", return_value=fake_agent), \
             patch.object(sys, "argv", ["agent", "--pair", "ABC123", "--backend-url", "https://central.example", "--mailbox-dir", "C:/mailbox"]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(agent_module.main(), 0)
        self.assertIn("oak-ridge", stdout.getvalue())
        self.assertNotIn("secret-value", stdout.getvalue() + stderr.getvalue())
        fake_agent.pair_once.assert_called_once_with("ABC123")

    def test_pair_invalid_code_raises_without_writing_response(self):
        opener = MagicMock(side_effect=RuntimeError("pairing API rejected request"))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                PairingAgent(folder, "https://central.example", opener).pair_once("expired")

    def test_pair_cli_returns_nonzero_on_unreachable_api(self):
        fake_agent = MagicMock()
        fake_agent.pair_once.side_effect = TimeoutError()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(agent_module, "PairingAgent", return_value=fake_agent), \
             patch.object(sys, "argv", ["agent", "--pair", "ABC123", "--backend-url", "https://central.example", "--mailbox-dir", "C:/mailbox"]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(agent_module.main(), 1)
        self.assertIn("unreachable", stderr.getvalue())
        self.assertNotIn("ABC123", stdout.getvalue() + stderr.getvalue())

    def test_pairing_response_matches_lua_schema(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            commands = Path(folder) / "permission-commands"
            commands.mkdir()
            request = commands / "pairing-request-0.xml"
            request.write_text('<serverPairingRequest code="abc123"/>', encoding="utf-8")
            agent = PairingAgent(folder, "https://central.example", opener)
            self.assertEqual(agent.process_once(), [request.name])
            self.assertEqual((commands / "server-pairing-response.xml").read_text(encoding="utf-8"),
                             '<?xml version=\'1.0\' encoding=\'utf-8\'?>\n<serverPairingResponse serverKey="oak-ridge" credential="secret-value" />')
            self.assertEqual(json.loads(opener.call_args.args[0].data), {"pairing_code": "ABC123"})

    def test_malformed_request_is_quarantined(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = Path(folder) / "permission-commands"
            commands.mkdir()
            request = commands / "pairing-request-0.xml"
            request.write_text("not xml", encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://localhost").process_once(), [])
            self.assertTrue((commands / "pairing-request-0.xml.failed").exists())

    def test_network_failure_leaves_request_for_retry(self):
        opener = MagicMock(side_effect=TimeoutError())
        with tempfile.TemporaryDirectory() as folder:
            commands = Path(folder) / "permission-commands"
            commands.mkdir()
            request = commands / "pairing-request-0.xml"
            request.write_text('<serverPairingRequest code="ABC123"/>', encoding="utf-8")
            PairingAgent(folder, "http://localhost", opener).process_once()
            self.assertTrue(request.exists())

    def test_event_is_posted_and_removed_after_acceptance(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "sin-fs25-01-1-player_connected.xml"
            event.write_text('<serverEvent event_id="event-1" event_type="player_connected" server_key="sin-fs25-01" server_credential="secret-value" save_id="1" unique_user_id="u1" display_name="Observed"/>', encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://central", opener).process_events_once(), [event.name])
            self.assertFalse(event.exists())
            request = opener.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/api/server/events"))
            sent = json.loads(request.data)
            self.assertEqual(sent["event_id"], "event-1")
            self.assertEqual(sent["payload"]["unique_user_id"], "u1")
            self.assertNotIn("secret-value", request.data.decode("utf-8"))
            self.assertEqual(request.headers["Authorization"], "Bearer secret-value")

    def test_malformed_event_is_quarantined(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "bad.xml"
            event.write_text("not xml", encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://localhost").process_events_once(), [])
            self.assertTrue((events / "bad.xml.failed").exists())

    def test_existing_quarantine_name_does_not_block_other_events(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            bad = events / "bad.xml"
            bad.write_text("not xml", encoding="utf-8")
            (events / "bad.xml.failed").write_text("previous", encoding="utf-8")
            good = events / "good.xml"
            good.write_text('<serverEvent event_id="event-2" event_type="heartbeat" server_key="oak-ridge" server_credential="secret-value" save_id="1"/>', encoding="utf-8")
            processed = PairingAgent(folder, "http://central", opener).process_events_once()
            self.assertEqual(processed, ["good.xml"])
            self.assertTrue((events / "bad.xml.failed.1").exists())

    def test_clock_policy_is_fetched_from_binding_and_written(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "serverBinding.xml").write_text('<serverBinding serverKey="sin-fs25-01" credential="secret-value"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text('<networkLocal savegameIndex="1"/>', encoding="utf-8")
            agent = PairingAgent(folder, "http://central", opener)
            # Response supplies the common pairing fixture, so use a direct helper mock.
            agent._get_clock_policy = MagicMock(return_value={"enabled": True, "timezone": "America/New_York",
                "target_game_minutes": 840, "normal_time_scale": 1, "catchup_time_scale": 15,
                "fast_catchup_threshold_minutes": 60, "fast_catchup_time_scale": 360,
                "ahead_time_scale": 0,
                "tolerance_minutes": 2, "hard_resync_threshold_minutes": 180,
                "hard_resync_enabled": False, "check_interval_seconds": 60,
                "save_key": "main", "generated_at": "now"})
            self.assertEqual(agent.process_clock_once(), 60)
            text = (root / "clock-policy.xml").read_text(encoding="utf-8")
            self.assertIn('target_game_minutes="840"', text)
            self.assertNotIn("secret-value", text)

    def test_agent_materializes_central_farm_operation_without_mongo(self):
        class OperationResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"operations": [{
                    "operation_id": "op-1", "operation_type": "ensure_farm",
                    "save_key": "main", "payload": {"farm_type": "system", "canonical_name": "SiN Harvest"}
                }]}).encode()

        opener = MagicMock(return_value=OperationResponse())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "serverBinding.xml").write_text('<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text('<networkLocal source="game" savegameIndex="1"/>', encoding="utf-8")
            agent = PairingAgent(root, "https://central", opener)
            self.assertEqual(agent.process_operations_once(), ["op-1"])
            command = root / "permission-commands/op-1.xml"
            self.assertIn('operation_type="ensure_farm"', command.read_text(encoding="utf-8"))
            self.assertIn('canonical_name="SiN Harvest"', command.read_text(encoding="utf-8"))
            self.assertNotIn("pymongo", "".join(path.read_text(encoding="utf-8") for path in [Path(agent_module.__file__)]))

    def test_permission_manifest_replace_retries_transient_windows_access_denied(self):
        class OperationResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"operations": [{
                    "operation_id": "op-retry", "operation_type": "ensure_farm",
                    "save_key": "main", "payload": {"canonical_name": "SiN Harvest"}
                }]}).encode()

        opener = MagicMock(return_value=OperationResponse())
        original_replace = Path.replace
        replace_attempts = {"manifest": 0}

        def replace(path, destination):
            if path.name == "manifest.tmp":
                replace_attempts["manifest"] += 1
                if replace_attempts["manifest"] < 3:
                    raise PermissionError(5, "Access is denied")
            return original_replace(path, destination)

        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "replace", replace), \
                patch.object(agent_module.time, "sleep") as sleep, patch.object(agent_module.os, "name", "nt"):
            root = Path(folder)
            (root / "serverBinding.xml").write_text(
                '<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text(
                '<networkLocal source="game" savegameIndex="1"/>', encoding="utf-8")
            agent = PairingAgent(root, "https://central", opener)
            self.assertEqual(agent.process_operations_once(), ["op-retry"])
            self.assertEqual(replace_attempts["manifest"], 3)
            self.assertTrue((root / "permission-commands/manifest.xml").exists())
            self.assertFalse((root / "permission-commands/manifest.tmp").exists())
            self.assertEqual(sleep.call_count, 2)

    def test_permission_manifest_permanent_failure_is_clear_and_cleans_temp(self):
        class OperationResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"operations": [{
                    "operation_id": "op-fail", "operation_type": "ensure_farm",
                    "save_key": "main", "payload": {}
                }]}).encode()

        opener = MagicMock(return_value=OperationResponse())
        original_replace = Path.replace

        def replace(path, destination):
            if path.name == "manifest.tmp":
                raise PermissionError(5, "Access is denied")
            return original_replace(path, destination)

        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "replace", replace), \
                patch.object(agent_module.time, "sleep"), patch.object(agent_module.os, "name", "nt"):
            root = Path(folder)
            (root / "serverBinding.xml").write_text(
                '<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text(
                '<networkLocal source="game" savegameIndex="1"/>', encoding="utf-8")
            destination = root / "permission-commands/manifest.xml"
            destination.parent.mkdir(parents=True)
            destination.write_text("old", encoding="utf-8")
            with self.assertRaises(PermissionError):
                PairingAgent(root, "https://central", opener).process_operations_once()
            self.assertEqual(destination.read_text(encoding="utf-8"), "old")
            self.assertFalse((destination.parent / "manifest.tmp").exists())

    def test_watch_failure_is_not_reported_as_pairing_failure(self):
        fake_agent = MagicMock()
        fake_agent.watch.side_effect = OSError("Access is denied")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(agent_module, "PairingAgent", return_value=fake_agent), \
                patch.object(sys, "argv", ["agent", "--watch", "--backend-url", "https://central.example", "--mailbox-dir", "C:/mailbox"]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(agent_module.main(), 1)
        self.assertNotIn("Pairing failed", stderr.getvalue())
        self.assertIn("Agent mailbox processing failed", stderr.getvalue())

    def test_failed_receipt_post_remains_queued_for_later_retry(self):
        failure = HTTPError("https://central/api/server/operation-receipts", 500, "central failure", {}, None)
        opener = MagicMock(side_effect=[failure, Response()])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "serverBinding.xml").write_text(
                '<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text(
                '<networkLocal source="game" savegameIndex="1"/>', encoding="utf-8")
            receipts = root / "permission-receipts"
            receipts.mkdir()
            receipt = receipts / "farm-op.xml"
            receipt.write_text(
                '<networkLocalReceipt operation_id="farm-op" operation_type="ensure_farm" '
                'server_id="server" save_id="main" status="applied" farm_id="1"/>', encoding="utf-8")
            agent = PairingAgent(root, "https://central", opener)
            self.assertEqual(agent.process_receipts_once(), [])
            self.assertTrue(receipt.exists())
            failure.close()
            self.assertEqual(agent.process_receipts_once(), [receipt.name])
            self.assertFalse(receipt.exists())
