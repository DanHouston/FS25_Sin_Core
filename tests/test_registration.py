import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from fs25_network_core.agent import PairingAgent
from fs25_network_core.authorization import AuthorizationManager


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.database.db.registration_codes.find.return_value.limit.return_value = []
        self.database.db.registration_codes.find_one.return_value = None
        self.database.db.game_identities.find.return_value.limit.return_value = []
        self.auth = AuthorizationManager(self.database)

    def test_request_creates_and_reuses_active_human_code(self):
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        first = self.auth.registration_request("server", "save", "stable-id", "Observed", "12")
        self.database.db.registration_codes.find_one.return_value = {
            "issued_at": int(datetime.now(timezone.utc).timestamp()), "expires_at": expires}
        second = self.auth.registration_request("server", "save", "stable-id", "Observed", "12")
        self.assertEqual(first["status"], "registration_required")
        self.assertEqual(first["code"], second["code"])
        self.assertEqual(len(first["code"]), 8)
        self.assertNotIn("code", self.database.db.registration_codes.update_one.call_args.args[1]["$set"])

    def test_registered_identity_refresh_returns_completed_without_new_code(self):
        self.database.db.game_identities.find.return_value.limit.return_value = [{
            "server_id": "server", "save_id": "save", "fs25_unique_user_id": "stable-id",
            "discord_id": "discord-user"}]
        with patch.object(self.auth, "create_registration_code") as create_code:
            result = self.auth.registration_request("server", "save", "stable-id", "Observed", "12")
        self.assertEqual(result, {"status": "registered", "fs25_unique_user_id": "stable-id"})
        create_code.assert_not_called()
        self.database.db.observed_fs25_identities.update_one.assert_not_called()

    def test_registration_code_upsert_preserves_first_issued_at_without_operator_conflict(self):
        class RegistrationCodes:
            def __init__(self):
                self.records = []
                self.updates = []

            def find_one(self, query):
                now = query["expires_at"]["$gt"]
                for record in self.records:
                    if all(record.get(key) == value for key, value in query.items()
                           if key != "expires_at") and record["expires_at"] > now:
                        return dict(record)
                return None

            def update_one(self, query, update, upsert=False):
                set_paths = set(update.get("$set", {}))
                insert_paths = set(update.get("$setOnInsert", {}))
                if set_paths & insert_paths:
                    raise AssertionError("Mongo update path conflict")
                self.updates.append(update)
                record = next((item for item in self.records
                               if all(item.get(key) == value for key, value in query.items())), None)
                inserted = record is None
                if inserted:
                    if not upsert:
                        raise AssertionError("expected upsert")
                    record = dict(query)
                    self.records.append(record)
                    record.update(update.get("$setOnInsert", {}))
                record.update(update.get("$set", {}))
                return type("Result", (), {"upserted_id": record.get("_id"), "modified_count": 1})()

        collection = RegistrationCodes()
        self.database.db.registration_codes = collection
        first_token, first_expiry = self.auth.create_registration_code("server", "save", "stable-id")
        first = collection.records[0]
        first_issued_at = first["issued_at"]
        first_updated_at = first["updated_at"]
        self.assertTrue(first_token)
        self.assertIn("issued_at", first)
        self.assertEqual(collection.updates[0]["$setOnInsert"]["issued_at"], first_issued_at)
        self.assertNotIn("issued_at", collection.updates[0]["$set"])

        first["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        second_token, second_expiry = self.auth.create_registration_code("server", "save", "stable-id")
        self.assertTrue(second_token)
        self.assertGreater(second_expiry, first_expiry)
        self.assertEqual(first["issued_at"], first_issued_at)
        self.assertGreater(first["updated_at"], first_updated_at)
        self.assertGreater(first["expires_at"], datetime.now(timezone.utc))
        self.assertNotIn("issued_at", collection.updates[1]["$set"])
        self.assertIn("issued_at", collection.updates[1].get("$setOnInsert", {}))

    def test_register_identity_resolves_code_without_server_argument(self):
        registration = {"_id": "r1", "server_id": "server", "save_id": "save", "fs25_unique_user_id": "stable-id"}
        self.database.db.registration_codes.find.return_value.limit.return_value = [registration]
        self.database.db.game_identities.find_one.side_effect = [None, None]
        result = MagicMock(modified_count=1)
        self.database.db.registration_codes.update_one.return_value = result
        with patch("fs25_network_core.authorization.hashlib.sha256") as unused:
            unused.return_value.hexdigest.return_value = "hash"
            self.assertEqual(self.auth.register_identity("discord", "ABC23456"), "stable-id")
        identity = self.database.db.game_identities.update_one.call_args.args[1]["$set"]
        self.assertEqual(identity["fs25_unique_user_id"], "stable-id")
        self.assertNotIn("approved_by", identity)

    def test_agent_registration_transport_writes_response_and_removes_request(self):
        class Response:
            status = 200
            def read(self):
                return json.dumps({"status": "registration_required", "code": "ABC12345",
                                   "expires_at": "later", "fs25_unique_user_id": "stable-id"}).encode()
            def __enter__(self): return self
            def __exit__(self, *args): pass

        def opener(request, timeout):
            self.assertEqual(request.full_url, "https://central/api/server/registration/request")
            self.assertEqual(request.headers["X-sin-server-key"], "server")
            return Response()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "serverBinding.xml").write_text('<serverBinding serverKey="server" credential="secret"/>', encoding="utf-8")
            request_dir = root / "registration-requests"
            request_dir.mkdir()
            (request_dir / "request-1.xml").write_text(
                '<registrationRequest request_id="request-1" fs25_save_id="1" fs25_unique_user_id="stable-id"/>', encoding="utf-8")
            agent = PairingAgent(root, "https://central", opener=opener)
            self.assertEqual(agent.process_registration_once(), ["request-1.xml"])
            response = ElementTree.parse(root / "registration-responses" / "request-1.xml").getroot()
            self.assertEqual(response.get("status"), "registration_required")
            self.assertFalse((request_dir / "request-1.xml").exists())

    def test_networklocal_uses_callback_based_get_files_for_registration_responses(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_NetworkLocal" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'getFiles(self.registrationResponseDirectory, "collectRegistrationResponseFile", self)',
            source,
        )
        self.assertNotRegex(source, r"getFiles\([^,\r\n]+\)")

    def test_registration_response_callback_normalizes_full_paths_for_load_and_delete(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_NetworkLocal" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn("return directory .. value", source)
        self.assertIn("local path = self:registrationResponsePath(filename)", source)
        process = source[source.index("function FS25SiNNetworkLocal:processRegistrationResponses()"):]
        process = process[:process.index("function FS25SiNNetworkLocal:enforceRegistration")]
        self.assertIn('XMLFile.load("networkLocalRegistrationResponse", path)', process)
        self.assertIn("deleteFile(path)", process)
        self.assertNotIn('XMLFile.load("networkLocalRegistrationResponse", self.registrationResponseDirectory ..', process)

    def test_networklocal_uses_targeted_warning_and_lifecycle_hooks(self):
        root = Path(__file__).parents[1] / "mods" / "FS25_SiN_NetworkLocal"
        source = (root / "NetworkLocal.lua").read_text(encoding="utf-8")
        event = (root / "events" / "SiNRegistrationWarningEvent.lua").read_text(encoding="utf-8")
        descriptor = (root / "modDesc.xml").read_text(encoding="utf-8")
        self.assertIn("FSBaseMission.onClientConnected", source)
        self.assertIn("FarmManager.playerQuitGame", source)
        self.assertIn("connection.sendEvent", source)
        self.assertIn("showBlinkingWarning", source)
        self.assertNotIn("sendTextMessage", source)
        self.assertIn("SiNRegistrationWarningEvent.lua", descriptor)
        self.assertIn("InitEventClass(SiNRegistrationWarningEvent", event)
        self.assertIn("streamWriteBool", event)
        self.assertIn("streamWriteString", event)
        self.assertIn("self.registrationWarning = nil", source)
        self.assertIn("self.registrationRequired = false", source)
        self.assertIn("self.registrationCode = nil", source)
        self.assertIn("if not self.registrationRequired or self.registrationCode == \"\"", source)
        self.assertIn("self:sendRegistrationState(uniqueId, state.status, state.code)", source)

    def test_networklocal_refreshes_required_registration_on_heartbeat_reconciliation(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_NetworkLocal" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn('elseif existing.status == "registration_required" and refreshRequired ~= true then', source)
        self.assertIn('elseif state.status == "registration_required" then', source)
        self.assertIn('self:queueRegistrationRequest(user, farm, true)', source)
        self.assertIn('or fileExists(self.registrationResponseDirectory .. requestId .. ".xml")', source)
        self.assertIn('self:enforceRegistration(user, farm)', source)
