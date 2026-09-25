import json
import inspect
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from unittest.mock import MagicMock

from fs25_network_core.farm_lifecycle import FarmLifecycle
from fs25_network_core.integration_campaign import _MemoryDatabase
from fs25_network_core.server_registry import ServerRegistry
from fs25_network_core.server_api import make_server


class ServerApiTests(unittest.TestCase):
    def test_standard_central_api_port_is_8787(self):
        self.assertEqual(inspect.signature(make_server).parameters["port"].default, 8787)

    def setUp(self):
        self.registry = MagicMock()
        self.registry.pair_code.return_value = ("local-dev", "one-time-response")
        self.database = MagicMock()
        self.server = make_server(self.database, "127.0.0.1", 0)
        self.server.RequestHandlerClass.registry = self.registry
        self.server.RequestHandlerClass.event_processor = MagicMock()
        self.server.RequestHandlerClass.event_processor.process.return_value = {"status": "accepted", "duplicate": False, "save_key": "main-save"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, payload):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/api/server/pair", json.dumps(payload), {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        return response.status, body

    def test_valid_pairing_returns_key_and_plaintext_only_in_response(self):
        status, body = self.request({"pairing_code": " ABC123 "})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"server_key": "local-dev", "credential": "one-time-response"})
        self.registry.pair_code.assert_called_once_with(" ABC123 ")

    def test_invalid_pairing_is_unauthorized(self):
        self.registry.pair_code.side_effect = ValueError("invalid")
        status, body = self.request({"pairing_code": "BAD"})
        self.assertEqual((status, body), (401, {"error": "invalid_pairing_code"}))

    def test_malformed_request_is_bad_request(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/api/server/pair", "{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        connection.close()

    def test_malformed_snapshot_logs_reason_before_scope_resolution(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        with self.assertLogs("fs25_network_core.server_api", level="WARNING") as logs:
            connection.request("POST", "/api/server/snapshot", "{not-json", headers={
                "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
                "Authorization": "Bearer secret"})
            response = connection.getresponse()
            response.read()
        self.assertEqual(response.status, 400)
        self.assertTrue(any("snapshot malformed" in message and "Expecting" in message
                            for message in logs.output))
        connection.close()

    def test_unknown_save_mapping_returns_actionable_reason(self):
        self.server.RequestHandlerClass.event_processor.registry.authenticate.return_value = {
            "server_key": "sin-fs25-01"}
        self.server.RequestHandlerClass.event_processor.registry.resolve_save.side_effect = ValueError(
            "Unknown or ambiguous FS25 save mapping")
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        with self.assertLogs("fs25_network_core.server_api", level="WARNING") as logs:
            connection.request("POST", "/api/server/snapshot", json.dumps({"fs25_save_id": "3", "snapshot": {}}),
                               headers={"Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
                                        "Authorization": "Bearer secret"})
            response = connection.getresponse()
            body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body["error"]), (400, "save_mapping_required"))
        self.assertIn("save mapping required", "\n".join(logs.output))

    def test_operation_receipt_missing_world_is_rejected_with_reason_and_logged(self):
        handler = self.server.RequestHandlerClass
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "sin-fs25-hobo"
        handler.farm_lifecycle = MagicMock()
        payload = {"fs25_save_id": "3", "world_id": "hobo-world", "receipt": {
            "operation_id": "old-receipt", "operation_type": "ensure_farm", "status": "applied",
            "save_id": "sin-fs25-hobo"}}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        with self.assertLogs("fs25_network_core.server_api", level="WARNING") as logs:
            connection.request("POST", "/api/server/operation-receipts", json.dumps(payload), {
                "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
                "Authorization": "Bearer secret"})
            response = connection.getresponse()
            body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body), (400, {
            "error": "invalid_operation_receipt",
            "reason": "operation receipt world generation is required"}))
        self.assertTrue(any("operation receipt rejected" in message
                            and "operation receipt world generation is required" in message
                            for message in logs.output))

    def test_malformed_operation_receipt_logs_bounded_reason(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        with self.assertLogs("fs25_network_core.server_api", level="WARNING") as logs:
            connection.request("POST", "/api/server/operation-receipts", "{not-json", headers={
                "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
                "Authorization": "Bearer secret"})
            response = connection.getresponse()
            body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body), (400, {
            "error": "invalid_operation_receipt", "reason": "malformed_json"}))
        self.assertTrue(any("operation receipt rejected" in message and "malformed_json" in message
                            for message in logs.output))

    def test_snapshot_records_and_activates_runtime_evidence(self):
        handler = self.server.RequestHandlerClass
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "sin-fs25-main"
        handler.farm_lifecycle = MagicMock()
        handler.farm_lifecycle.record_snapshot.return_value = {
            "received_at": datetime.now(timezone.utc)}
        payload = {"fs25_save_id": "3", "snapshot": {
            "source": "game", "world_id": "hobo-world", "savegame_index": 3,
            "runtime_generation": 9, "session": "hobo-session", "sequence": 1}}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/api/server/snapshot", json.dumps(payload), {
            "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
            "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "accepted")
        handler.event_processor.registry.validate_runtime_snapshot.assert_called_once_with(
            "sin-fs25-01", "sin-fs25-main", payload["snapshot"])
        handler.event_processor.registry.activate_runtime.assert_called_once_with(
            "sin-fs25-01", "sin-fs25-main", payload["snapshot"])

    def test_snapshot_switches_active_save_and_rejects_delayed_previous_runtime(self):
        database = _MemoryDatabase()
        database.db.sin_servers.insert_one({"_id": "sin-fs25-01", "server_key": "sin-fs25-01",
                                             "enabled": True,
                                             "credential_hash": __import__("hashlib").sha256(b"secret").hexdigest()})
        for save_key, save_id in (("sin-fs25-main", "1"), ("sin-fs25-hobo", "3")):
            database.db.sin_saves.insert_one({"_id": f"sin-fs25-01:{save_key}",
                                              "server_key": "sin-fs25-01", "save_key": save_key,
                                              "fs25_save_id": save_id})
        registry = ServerRegistry(database)
        processor = MagicMock()
        processor.registry = registry
        handler = self.server.RequestHandlerClass
        handler.registry = registry
        handler.event_processor = processor
        handler.farm_lifecycle = FarmLifecycle(database)

        def post(save_id, save_key, world_id, runtime_generation):
            payload = {"fs25_save_id": save_id, "snapshot": {
                "source": "game", "savegame_index": int(save_id), "world_id": world_id,
                "runtime_generation": runtime_generation, "session": f"session-{runtime_generation}",
                "sequence": 1, "farms": {}, "players": {}, "farmlands": {}}}
            connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            connection.request("POST", "/api/server/snapshot", json.dumps(payload), {
                "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
                "Authorization": "Bearer secret"})
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            return response.status, body

        self.assertEqual(post("3", "sin-fs25-hobo", "hobo-world", 20)[0], 200)
        self.assertEqual(post("1", "sin-fs25-main", "courtright-world", 19)[0], 400)
        self.assertEqual(registry.active_runtime("sin-fs25-01")["save_key"], "sin-fs25-hobo")
        self.assertIsNone(database.db.server_snapshots.find_one({
            "server_key": "sin-fs25-01", "save_key": "sin-fs25-main"}))

        self.assertEqual(post("1", "sin-fs25-main", "courtright-world", 21)[0], 200)
        self.assertEqual(registry.active_runtime("sin-fs25-01")["save_key"], "sin-fs25-main")
        self.assertEqual(registry.worlds.active_id("sin-fs25-01", "sin-fs25-hobo"), "hobo-world")
        self.assertEqual(registry.worlds.active_id("sin-fs25-01", "sin-fs25-main"), "courtright-world")

    def test_event_endpoint_forwards_to_central_processor(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        event = {"event_id": "e1", "event_type": "heartbeat", "server_key": "sin-fs25-01",
                 "server_credential": "secret", "save_id": "1", "payload": {}}
        connection.request("POST", "/api/server/events", json.dumps(event), {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.server.RequestHandlerClass.event_processor.process.assert_called_once_with(event)
        connection.close()

    def test_event_endpoint_accepts_credential_in_auth_header(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        event = {"event_id": "e-header", "event_type": "heartbeat", "server_key": "sin-fs25-01",
                 "save_id": "1", "payload": {}}
        connection.request("POST", "/api/server/events", json.dumps(event), {
            "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
            "Authorization": "Bearer secret"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        forwarded = self.server.RequestHandlerClass.event_processor.process.call_args.args[0]
        self.assertEqual(forwarded["server_credential"], "secret")
        connection.close()

    def test_clock_endpoint_authenticates_and_returns_policy(self):
        self.server.RequestHandlerClass.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        self.server.RequestHandlerClass.event_processor.registry.resolve_save.return_value = "main"
        self.server.RequestHandlerClass.event_processor.registry.clock_policy.return_value = {
            "enabled": True, "timezone": "America/New_York", "offset_minutes": -360,
            "normal_time_scale": 1, "catchup_time_scale": 15,
            "fast_catchup_threshold_minutes": 60, "fast_catchup_time_scale": 360,
            "tolerance_minutes": 2,
            "ahead_time_scale": 0,
            "hard_resync_threshold_minutes": 180, "hard_resync_enabled": True,
            "check_interval_seconds": 60}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/api/server/clock?fs25_save_id=1", headers={
            "X-SiN-Server-Key": "sin-fs25-01", "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body["save_key"], "main")
        self.server.RequestHandlerClass.event_processor.registry.authenticate.assert_called_with("sin-fs25-01", "secret")
        connection.close()
    def test_authenticated_operations_endpoint_returns_transport_safe_fields(self):
        handler = self.server.RequestHandlerClass
        handler.farm_lifecycle = MagicMock()
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "main"
        handler.farm_lifecycle.operations_for.return_value = [{
            "_id": "op", "operation_id": "op", "operation_type": "ensure_farm",
            "server_key": "sin-fs25-01", "save_key": "main", "payload": {"canonical_name": "SiN Harvest"},
            "state": "pending", "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)}]
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/api/server/operations?fs25_save_id=1", headers={
            "X-SiN-Server-Key": "sin-fs25-01", "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body["operations"][0]["operation_id"], "op")
        self.assertNotIn("created_at", body["operations"][0])
        connection.close()

    def test_farmland_receipt_uses_receipt_gated_farm_lifecycle_not_legacy_land_path(self):
        handler = self.server.RequestHandlerClass
        handler.farm_lifecycle = MagicMock()
        handler.farm_lifecycle.accept_receipt.return_value = {"operation_id": "land-op", "state": "succeeded"}
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "main"
        payload = {"fs25_save_id": "1", "world_id": "test-world", "receipt": {"operation_id": "land-op",
            "operation_type": "assign_farmland", "status": "applied", "farmland_id": "12",
            "farm_id": "2", "owner_before_farm_id": "0", "owner_farm_id": "2", "world_id": "test-world"}}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/api/server/operation-receipts", json.dumps(payload), {
            "Content-Type": "application/json", "X-SiN-Server-Key": "sin-fs25-01",
            "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, body["operation_state"]), (200, "succeeded"))
        handler.farm_lifecycle.require_current_world.assert_called_once_with("sin-fs25-01", "main", "test-world")
        handler.farm_lifecycle.accept_receipt.assert_called_once_with("sin-fs25-01", "main", payload["receipt"], "test-world")
        handler.event_processor.authorization.acknowledge_land.assert_not_called()

    def test_manager_authority_endpoint_includes_persisted_pending_manager_assignment(self):
        handler = self.server.RequestHandlerClass
        handler.farm_lifecycle = MagicMock()
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "sin-fs25-main"
        # Use a single-pass cursor, matching PyMongo, so the endpoint must
        # materialize the shared relationship projection before splitting it.
        handler.event_processor.authorization.db.memberships.find.return_value = iter([{
            "game_player_id": "stable-player", "farm_id": 2,
            "state": "pending", "desired_role": "farm_manager", "applied_role": None,
        }, {
            "game_player_id": "stable-player", "farm_id": 99,
            "source_farm_id": 2,
            "state": "pending", "desired_role": "contractor", "applied_role": None,
        }])
        handler.farm_lifecycle._repair_contractor_authorizations = MagicMock()
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/api/server/manager-authority?fs25_save_id=1&world_id=test-world", headers={
            "X-SiN-Server-Key": "sin-fs25-01", "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["managers"], [{"game_player_id": "stable-player", "farm_id": 2}])
        self.assertEqual(body["contractors"], [{"game_player_id": "stable-player", "farm_id": 99,
                                                 "source_farm_id": 2}])
        handler.farm_lifecycle._repair_contractor_authorizations.assert_called_once_with(
            "sin-fs25-01", "sin-fs25-main")
        query = handler.event_processor.authorization.db.memberships.find.call_args.args[0]
        self.assertEqual(query["state"], {"$in": ["pending", "active"]})
        self.assertEqual(query["desired_role"], {"$in": ["farm_manager", "contractor"]})
