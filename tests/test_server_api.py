import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock

from fs25_network_core.server_api import make_server


class ServerApiTests(unittest.TestCase):
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

    def test_manager_authority_endpoint_includes_persisted_pending_manager_assignment(self):
        handler = self.server.RequestHandlerClass
        handler.event_processor.registry.authenticate.return_value = {"server_key": "sin-fs25-01"}
        handler.event_processor.registry.resolve_save.return_value = "sin-fs25-main"
        handler.event_processor.authorization.db.memberships.find.return_value = [{
            "game_player_id": "stable-player", "farm_id": 2,
            "state": "pending", "desired_role": "farm_manager", "applied_role": None,
        }]
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", "/api/server/manager-authority?fs25_save_id=1", headers={
            "X-SiN-Server-Key": "sin-fs25-01", "Authorization": "Bearer secret"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["managers"], [{"game_player_id": "stable-player", "farm_id": 2}])
        query = handler.event_processor.authorization.db.memberships.find.call_args.args[0]
        self.assertEqual(query["state"], {"$in": ["pending", "active"]})
        self.assertEqual(query["desired_role"], "farm_manager")
