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
        issued_at = self.database.db.registration_codes.update_one.call_args.args[1]["$setOnInsert"]["issued_at"]
        self.database.db.registration_codes.find_one.return_value = {
            "issued_at": issued_at, "expires_at": expires}
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
                now = query.get("expires_at", {}).get("$gt")
                for record in self.records:
                    if not all(record.get(key) == value for key, value in query.items()
                               if key != "expires_at" and not isinstance(value, dict)):
                        continue
                    if now is None or record["expires_at"] > now:
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
        self.assertIn("issued_at", collection.updates[1]["$set"])

        # A later active reuse derives the same token from the refreshed
        # issuance timestamp, rather than the expired record's old timestamp.
        reused = self.auth.registration_request("server", "save", "stable-id")
        self.assertEqual(reused["code"], second_token)

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

    def test_server_runtime_uses_callback_based_get_files_for_registration_responses(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'getFiles(self.registrationResponseDirectory, "collectRegistrationResponseFile", self)',
            source,
        )
        self.assertNotRegex(source, r"getFiles\([^,\r\n]+\)")

    def test_server_runtime_uses_giants_authorized_canonical_mailbox_root(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn('local SERVER_MAILBOX_NAME = "FS25_SiN_Server"', source)
        self.assertIn('"modSettings/" .. SERVER_MAILBOX_NAME .. "/"', source)
        self.assertNotIn('"modSettings/FS25SiNServer/"', source)

    def test_updater_migration_is_complete_and_conflict_safe(self):
        source = (Path(__file__).parents[1] / "scripts" / "Update-SiN.ps1").read_text(encoding="utf-8")
        self.assertIn('[switch]$MigrateLegacyMailbox', source)
        self.assertIn('"FS25_SiN_NetworkLocal"', source)
        self.assertIn('serverBinding.xml', source)
        self.assertIn('Move-Item -LiteralPath $legacy -Destination $destination', source)
        self.assertIn('FS25_SiN_NetworkLocal', source)
        self.assertIn('Both legacy and canonical mailbox directories contain state', source)
        self.assertIn('Invoke-LegacyMailboxMigration -Destination $MailboxDir', source)

    def test_registration_response_callback_normalizes_full_paths_for_load_and_delete(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn("return directory .. value", source)
        self.assertIn("local path = self:registrationResponsePath(filename)", source)
        process = source[source.index("function FS25SiNServer:processRegistrationResponses()"):]
        process = process[:process.index("function FS25SiNServer:enforceRegistration")]
        self.assertIn('XMLFile.load("networkLocalRegistrationResponse", path)', process)
        self.assertIn("deleteFile(path)", process)
        self.assertNotIn('XMLFile.load("networkLocalRegistrationResponse", self.registrationResponseDirectory ..', process)

    def test_server_runtime_uses_targeted_warning_and_lifecycle_hooks(self):
        root = Path(__file__).parents[1] / "mods" / "FS25_SiN_Server"
        source = (root / "NetworkLocal.lua").read_text(encoding="utf-8")
        event = (root / "events" / "SiNRegistrationWarningEvent.lua").read_text(encoding="utf-8")
        descriptor = (root / "modDesc.xml").read_text(encoding="utf-8")
        self.assertIn("<title><en>SiN (SimNet) Server</en></title>", descriptor)
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

    def test_server_runtime_snapshot_does_not_export_dedicated_server_pseudo_user(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        snapshot = source[source.index("function FS25SiNServer:exportSnapshot()"):]
        self.assertIn("local pseudo = self:isDedicatedServerUser(user, userFarm)", snapshot)
        self.assertIn("if not pseudo then", snapshot)
        self.assertIn("currentPlayers[identityKey]", snapshot)

    def test_server_runtime_farm_operations_use_verified_authoritative_apis(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("g_farmManager:createFarm(farmName, colorIndex, \"\", nil)", source)
        self.assertNotIn("g_farmManager:createFarm(farmName, 1, \"\", nil)", source)
        self.assertNotIn("g_farmManager:createFarm(farmName, 0, \"\", nil)", source)
        self.assertIn("type(Farm.COLORS) ~= \"table\"", source)
        self.assertIn("nextAvailableFarmId", source)
        self.assertIn("selectFarmColor(preferredFarmId)", source)
        self.assertIn("not used[preferredFarmId]", source)
        self.assertIn("g_farmManager:getFarmById(farmId)", source)
        self.assertIn("g_farmlandManager:setLandOwnership(farmlandId, farmId)", source)
        self.assertIn("FarmlandStateEvent.new(farmlandId, farmId, 0)", source)
        self.assertIn("g_server:broadcastEvent", source)
        self.assertNotRegex(source, r"setLandOwnership\([^\n]*,[^\n]*,[^\n]*\)")
        self.assertIn('operationType == "ensure_farm" or operationType == "provision_farm"', source)

    def test_server_runtime_color_policy_never_mutates_existing_farm_colors(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        selector = source[source.index("function FS25SiNServer:selectFarmColor") :]
        selector = selector[:selector.index("function FS25SiNServer:nextAvailableFarmId")]
        self.assertIn("used[farm.color] = true", selector)
        self.assertIn("return preferredFarmId, nil", selector)
        self.assertIn("return nil, \"no unused FS25 farm color is available\"", selector)
        self.assertNotIn("setColor", selector)

    def test_server_runtime_land_receipt_requires_client_replication_event(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        helper = source[source.index("function FS25SiNServer:setAndReplicateLandOwnership") :]
        helper = helper[:helper.index("function FS25SiNServer:processFarmProvisionCommand")]
        self.assertIn("getFarmlandOwner(farmlandId)", helper)
        self.assertIn("FarmlandStateEvent.new(farmlandId, farmId, 0)", helper)
        self.assertIn("farmland replication event is unavailable", helper)

    def test_server_runtime_rejects_malformed_farm_visual_state_before_operations(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:isFarmVisualStateValid(farm)", source)
        self.assertIn("farm.color < 1", source)
        self.assertIn("Farm.COLORS[farm.color]", source)
        self.assertIn("farm.getIconSliceId", source)
        self.assertIn("farm.getIconUVs", source)
        self.assertIn("self:isFarmVisualStateValid(farm)", source)
        self.assertIn('error("farm visual state invalid: " .. tostring(visualStateError))', source)
        self.assertIn("invalid visual state farmId=", source)

    def test_server_runtime_refreshes_required_registration_on_heartbeat_reconciliation(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn('elseif existing.status == "registration_required" and refreshRequired ~= true then', source)
        self.assertIn('elseif state.status == "registration_required" then', source)
        self.assertIn('self:queueRegistrationRequest(user, farm, true)', source)
        self.assertIn('or fileExists(self.registrationResponseDirectory .. requestId .. ".xml")', source)
        self.assertIn('self:enforceRegistration(user, farm)', source)

    def test_server_runtime_restores_runtime_manager_permissions_and_uses_native_replication(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:enforceAuthorizedManagerState(user, farm, authorized)", source)
        self.assertIn("farm.getUserPermissions", source)
        self.assertIn("farm:setUserPermission(userId, permission, true)", source)
        self.assertIn("PlayerPermissionsEvent.sendEvent", source)
        self.assertIn("self:enforceAuthorizedManagerState(user, farm, authorized)", source)
        self.assertIn("self:enforceAuthorizedManagerState(user, farm, isAuthorized)", source)
        self.assertIn("beforeManager", source)
        self.assertIn("afterManager", source)

    def test_server_runtime_has_no_fixed_system_farm_id_assumption(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertNotIn("getFarmById(2)", source)
        self.assertIn("function FS25SiNServer:resolveConfiguredSystemFarm()", source)
        self.assertIn("self.systemFarmName = farmName", source)
        self.assertNotIn("System farm diagnostic farm=2", source)

    def test_server_runtime_activity_sampling_is_local_and_excludes_pseudo_user(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("self.activityMovementTolerance = 0.5", source)
        self.assertIn("function FS25SiNServer:processActivitySamples(dt)", source)
        self.assertIn('self:emitServerEvent("player_activity_minute"', source)
        self.assertIn("getWorldTranslation, player.rootNode", source)
        self.assertIn("state.inactivityMinutes <= 10", source)
        self.assertIn("self.activityStates[uniqueId] = nil", source)
        self.assertIn("self:isDedicatedServerUser(user, farm)", source)

    def test_activity_event_uses_existing_mailbox_descriptor(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('self:emitServerEvent("player_activity_minute"', source)
        self.assertIn('eventType ~= "player_activity_minute"', source)
        self.assertIn('self.eventDirectory .. eventId .. ".xml"', source)
