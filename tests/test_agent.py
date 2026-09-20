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

    def test_activity_minute_event_uses_existing_event_transport_and_is_removed(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "activity-1.xml"
            event.write_text(
                '<serverEvent event_id="activity-1" event_type="player_activity_minute" '
                'server_key="sin-fs25-01" server_credential="secret-value" save_id="1" '
                'unique_user_id="stable" user_id="2" farm_id="1" display_name="Player" '
                'session_id="session-1" minute_sequence="1" activity_bucket="active" '
                'inactive_minutes="0" duration_seconds="60"/>', encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://central", opener).process_events_once(), [event.name])
            self.assertFalse(event.exists())
            sent = json.loads(opener.call_args.args[0].data)
            self.assertEqual(sent["event_type"], "player_activity_minute")
            self.assertEqual(sent["payload"]["activity_bucket"], "active")

    def test_activity_minute_transport_failure_leaves_event_for_retry(self):
        opener = MagicMock(side_effect=TimeoutError())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "activity-retry.xml"
            event.write_text(
                '<serverEvent event_id="activity-retry" event_type="player_activity_minute" '
                'server_key="server" server_credential="secret" save_id="1" unique_user_id="stable" '
                'session_id="session" minute_sequence="1" activity_bucket="idle" inactive_minutes="1"/>',
                encoding="utf-8")
            PairingAgent(folder, "http://central", opener).process_events_once()
            self.assertTrue(event.exists())

    def test_bounded_batch_uses_session_order_across_lexically_split_mailbox(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            common = ('server_key="server" server_credential="secret" save_id="1" '
                      'unique_user_id="u" display_name="Player" session_id="sess" ')
            files = {
                "s-100-1-player_connected.xml": '<serverEvent event_id="connect" event_type="player_connected" ' + common + '/>',
                "s-100-2-player_disconnected.xml": '<serverEvent event_id="disconnect" event_type="player_disconnected" ' + common + 'final_minute_sequence="2"/>',
                "s-activity-sess-u-1.xml": '<serverEvent event_id="minute-1" event_type="player_activity_minute" ' + common + 'minute_sequence="1" activity_bucket="active" inactive_minutes="0"/>',
                "s-activity-sess-u-2.xml": '<serverEvent event_id="minute-2" event_type="player_activity_minute" ' + common + 'minute_sequence="2" activity_bucket="idle" inactive_minutes="1"/>',
            }
            for name, content in files.items():
                (events / name).write_text(content, encoding="utf-8")
            agent = PairingAgent(folder, "http://central", opener)
            first = agent.process_events_once(max_events=2)
            self.assertNotIn("s-100-2-player_disconnected.xml", first)
            self.assertTrue((events / "s-100-2-player_disconnected.xml").exists())
            second = agent.process_events_once(max_events=2)
            self.assertIn("s-activity-sess-u-2.xml", second)
            self.assertIn("s-100-2-player_disconnected.xml", second)

    def test_restart_does_not_require_removed_earlier_minutes(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            common = ('server_key="server" server_credential="secret" save_id="1" '
                      'unique_user_id="u" display_name="Player" session_id="runtime-session" ')
            (events / "minute-2.xml").write_text(
                '<serverEvent event_id="minute-2" event_type="player_activity_minute" ' + common +
                'minute_sequence="2" activity_bucket="idle" inactive_minutes="1"/>', encoding="utf-8")
            (events / "disconnect.xml").write_text(
                '<serverEvent event_id="disconnect" event_type="player_disconnected" ' + common +
                'final_minute_sequence="2"/>', encoding="utf-8")
            processed = PairingAgent(folder, "http://central", opener).process_events_once()
            self.assertEqual(processed, ["minute-2.xml", "disconnect.xml"])
            self.assertEqual([json.loads(call.args[0].data)["event_id"] for call in opener.call_args_list],
                             ["minute-2", "disconnect"])

    def test_malformed_lookahead_minute_does_not_abort_event_scheduler(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            common = ('server_key="server" server_credential="secret" save_id="1" '
                      'unique_user_id="u" session_id="s" ')
            (events / "00-disconnect.xml").write_text(
                '<serverEvent event_id="disconnect" event_type="player_disconnected" ' + common +
                'final_minute_sequence="2"/>', encoding="utf-8")
            (events / "01-invalid-minute.xml").write_text(
                '<serverEvent event_id="invalid" event_type="player_activity_minute" ' + common +
                'minute_sequence="not-a-number"/>', encoding="utf-8")
            (events / "02-minute.xml").write_text(
                '<serverEvent event_id="minute-2" event_type="player_activity_minute" ' + common +
                'minute_sequence="2"/>', encoding="utf-8")
            agent = PairingAgent(folder, "http://central", opener)
            processed = agent.process_events_once(max_events=1)
            self.assertEqual(processed, ["02-minute.xml"])
            self.assertTrue((events / "01-invalid-minute.xml").exists())
            self.assertTrue((events / "00-disconnect.xml").exists())

    def test_retryable_failure_blocks_only_its_scoped_session(self):
        def response(request, timeout=10):
            if json.loads(request.data)["event_id"] == "blocked":
                raise TimeoutError()
            return Response()
        opener = MagicMock(side_effect=response)
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            for name, event_id, user, session in (
                    ("a.xml", "blocked", "u1", "s1"), ("b.xml", "blocked-next", "u1", "s1"),
                    ("c.xml", "other", "u2", "s2")):
                (events / name).write_text(
                    f'<serverEvent event_id="{event_id}" event_type="heartbeat" server_key="server" '
                    f'server_credential="secret" save_id="1" unique_user_id="{user}" session_id="{session}"/>',
                    encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://central", opener).process_events_once(max_events=3),
                             ["c.xml"])
            self.assertEqual(opener.call_count, 2)

    def test_max_events_bounds_success_and_permanent_rejection_attempts(self):
        rejected = HTTPError("http://central", 422, "bad", {}, None)
        opener = MagicMock(side_effect=[rejected, Response(), Response()])
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            for index in range(3):
                (events / f"{index}.xml").write_text(
                    f'<serverEvent event_id="event-{index}" event_type="heartbeat" server_key="server" '
                    'server_credential="secret" save_id="1"/>', encoding="utf-8")
            PairingAgent(folder, "http://central", opener).process_events_once(max_events=2)
            self.assertEqual(opener.call_count, 2)

    def test_watch_polls_receipts_before_operations(self):
        agent = PairingAgent("C:/mailbox", "http://central")
        calls = []
        agent.process_once = lambda: calls.append("pair")
        agent.process_registration_once = lambda: calls.append("registration")
        agent.process_receipts_once = lambda: calls.append("receipts")
        agent.process_operations_once = lambda: calls.append("operations")
        agent.process_manager_authority_once = lambda: calls.append("authority")
        agent.process_snapshot_once = lambda: calls.append("snapshot")
        agent.process_clock_once = lambda: 60
        agent.process_events_once = lambda limit: calls.append("events")
        with patch.object(agent_module.time, "sleep"):
            agent.watch(stop=lambda: bool(calls))
        self.assertLess(calls.index("receipts"), calls.index("operations"))

    def test_chat_event_uses_authenticated_event_transport(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "chat-1.xml"
            event.write_text(
                '<serverEvent event_id="chat-1" event_type="chat_message" '
                'server_key="server" server_credential="secret" save_id="1" '
                'message_id="message-1" message="hello" source="fs25" unique_user_id="stable"/>',
                encoding="utf-8")
            self.assertEqual(PairingAgent(folder, "http://central", opener).process_events_once(), [event.name])
            self.assertFalse(event.exists())
            sent = json.loads(opener.call_args.args[0].data)
            self.assertEqual(sent["event_type"], "chat_message")
            self.assertEqual(sent["payload"]["message"], "hello")

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

    def test_durable_local_receipt_suppresses_operation_recreation(self):
        class OperationResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"operations": [{"operation_id": "already-acked",
                    "operation_type": "ensure_farm", "save_key": "main",
                    "payload": {"canonical_name": "SiN Harvest"}}]}).encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "serverBinding.xml").write_text('<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            (root / "snapshot.xml").write_text('<networkLocal source="game" savegameIndex="1"/>', encoding="utf-8")
            receipt_dir = root / "permission-receipts"
            receipt_dir.mkdir()
            (receipt_dir / "already-acked.xml").write_text(
                '<networkLocalReceipt operation_id="already-acked" status="applied"/>', encoding="utf-8")
            agent = PairingAgent(root, "https://central", MagicMock(return_value=OperationResponse()))
            self.assertEqual(agent.process_operations_once(), [])
            self.assertFalse((root / "permission-commands/already-acked.xml").exists())

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

        def windows_access_denied():
            error = PermissionError(13, "Access is denied")
            error.winerror = 5
            return error

        def replace(path, destination):
            if path.name == "manifest.tmp":
                replace_attempts["manifest"] += 1
                if replace_attempts["manifest"] < 3:
                    raise windows_access_denied()
            return original_replace(path, destination)

        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "replace", replace), \
                patch.object(agent_module.time, "sleep") as sleep:
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

        def windows_access_denied():
            error = PermissionError(13, "Access is denied")
            error.winerror = 5
            return error

        def replace(path, destination):
            if path.name == "manifest.tmp":
                raise windows_access_denied()
            return original_replace(path, destination)

        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "replace", replace), \
                patch.object(agent_module.time, "sleep"):
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

    def test_event_processing_is_bounded_for_a_large_durable_backlog(self):
        opener = MagicMock(return_value=Response())
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            xml = ('<serverEvent event_id="event-{0:05d}" event_type="player_activity_minute" '
                   'server_key="server" server_credential="secret" save_id="1" '
                   'unique_user_id="stable" session_id="session" minute_sequence="{0}" '
                   'activity_bucket="idle" inactive_minutes="1"/>')
            for number in range(1, 10_001):
                (events / f"event-{number:05d}.xml").write_text(xml.format(number), encoding="utf-8")
            agent = PairingAgent(folder, "https://central", opener)
            self.assertEqual(len(agent.process_events_once(max_events=50)), 50)
            self.assertEqual(len(list(events.glob("*.xml"))), 9_950)
            # A later bounded call continues making progress without asking
            # the control-plane loop to drain the historical queue first.
            self.assertEqual(len(agent.process_events_once(max_events=50)), 50)
            self.assertEqual(len(list(events.glob("*.xml"))), 9_900)

    def test_map_geometry_event_preserves_nested_field_points(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.mkdir()
            event = events / "map.xml"
            event.write_text(
                '<serverEvent event_id="map-1" event_type="map_geometry" server_key="server" '
                'server_credential="secret" save_id="1" schema_version="1" map_id="map" '
                'map_title="Map" world_width="100" world_depth="100" image_width="32" '
                'image_height="32" overview_asset_identity="runtime-generated:map" version="1" '
                'image_y_inverted="true"><fields><field field_id="22" farmland_id="22" '
                'area_ha="1"><points><point x="-10" z="-10"/><point x="10" z="-10"/>'
                '<point x="0" z="10"/></points></field></fields></serverEvent>',
                encoding="utf-8")
            parsed = PairingAgent._parse_event_file(event)
            self.assertEqual(parsed["payload"]["map"]["fields"]["22"]["rings"][0][1], ["10", "-10"])

    def test_map_geometry_event_preserves_farmland_identity_and_optional_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            event = Path(folder) / "map.xml"
            event.write_text(
                '<serverEvent event_id="map-1" event_type="map_geometry" server_key="server" '
                'server_credential="secret" save_id="1" schema_version="1" source_generation="4" '
                'map_id="map" map_title="Map" world_width="100" world_depth="100" image_width="32" '
                'image_height="32" overview_asset_identity="runtime-generated:map" version="1" '
                'image_y_inverted="true" coordinate_system="giants-centered-xz"><fields/>'
                '<farmlands><farmland farmland_id="22"><points><point x="-10" z="-10"/>'
                '<point x="10" z="-10"/><point x="10" z="10"/></points></farmland>'
                '<farmland farmland_id="47"/></farmlands></serverEvent>',
                encoding="utf-8")
            parsed = PairingAgent._parse_event_file(event)
            payload = parsed["payload"]
            self.assertEqual(payload["source_generation"], "4")
            self.assertEqual(payload["map"]["farmland_ids"], ["22", "47"])
            self.assertEqual(payload["map"]["farmlands"]["22"]["rings"][0][0], ["-10", "-10"])
            self.assertNotIn("47", payload["map"]["farmlands"])

    def test_watch_services_control_plane_before_each_bounded_event_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = PairingAgent(folder, "https://central", event_batch_size=7)
            calls = []
            for name in ("process_once", "process_registration_once", "process_operations_once",
                         "process_receipts_once", "process_manager_authority_once",
                         "process_snapshot_once", "process_clock_once"):
                method = MagicMock(side_effect=lambda name=name: calls.append(name) or 60)
                setattr(agent, name, method)
            agent.process_events_once = MagicMock(side_effect=lambda size: calls.append(("events", size)) or [])
            stop = MagicMock(side_effect=[False, True])
            with patch.object(agent_module.time, "monotonic", return_value=100), \
                    patch.object(agent_module.time, "sleep"):
                agent.watch(interval=0, stop=stop)
            self.assertLess(calls.index("process_registration_once"), calls.index(("events", 7)))
