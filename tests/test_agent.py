import json
import io
import sys
import tempfile
import unittest
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

    def test_malformed_event_is_quarantined(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "bad.xml"
            event.write_text("not xml", encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://localhost").process_events_once(), [])
            self.assertTrue((events / "bad.xml.failed").exists())

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
