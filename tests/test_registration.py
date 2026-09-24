import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from fs25_network_core.agent import PairingAgent
from fs25_network_core.authorization import AuthorizationManager
from fs25_network_core.event_processing import CentralEventProcessor
from fs25_network_core.integration_campaign import _MemoryDatabase, _MemoryCollection
from fs25_network_core.lua_validation import validate_fs25_lua_source
from pymongo.errors import DuplicateKeyError


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.database.db.registration_codes.find.return_value.limit.return_value = []
        self.database.db.registration_codes.find_one.return_value = None
        self.database.db.game_identities.find.return_value.limit.return_value = []
        self.auth = AuthorizationManager(self.database)

    def test_all_packaged_fs25_lua_sources_pass_runtime_dialect_gate(self):
        root = Path(__file__).parents[1] / "mods" / "FS25_SiN_Server"
        for path in sorted(root.rglob("*.lua")):
            with self.subTest(path=path):
                validate_fs25_lua_source(path.read_bytes(), str(path.relative_to(root)))

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
        replacement = collection.records[0]
        self.assertEqual(collection.updates[1]["$set"]["issued_at"], replacement["issued_at"])
        self.assertGreaterEqual(replacement["issued_at"], first_issued_at)
        self.assertGreater(replacement["updated_at"], first_updated_at)
        self.assertGreater(replacement["expires_at"], datetime.now(timezone.utc))
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

    def test_new_save_world_identity_retries_after_save_directory_becomes_available(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        identity = source[source.index("function FS25SiNServer:initializeWorldIdentity()"):]
        identity = identity[:identity.index("function FS25SiNServer:shortIdentity")]
        self.assertIn("self.worldIdentityRetryable = true", identity)
        self.assertIn("self.worldIdentityRetryElapsed", source)
        self.assertIn("self.worldIdentityRetryInterval = 5000", source)
        self.assertIn("existing save marker is malformed; refusing world-scoped operations", identity)
        self.assertIn("self.worldIdentityRetryable = false", identity)
        self.assertIn('local path = tostring(saveDirectory) .. "FS25_SiN_Server_world.xml"', identity)
        self.assertNotIn('self.directory .. "FS25_SiN_Server_world.xml"', identity)
        self.assertIn("FS25 save lifecycle hook is unavailable; refusing unpersisted world identity", identity)
        update = source[source.index("function FS25SiNServer:update(dt)"):]
        self.assertIn("self.worldIdentitySaveHookInstalled ~= true", update)
        self.assertIn("self.worldIdentityRetryable == true", update)
        self.assertIn("self:initializeWorldIdentity()", update)

    def test_world_identity_uses_actual_fs25_save_lifecycle_and_observable_marker_write(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        identity = source[source.index("function FS25SiNServer:installWorldIdentityPersistenceHook()"):]
        identity = identity[:identity.index("function FS25SiNServer:shortIdentity")]
        self.assertIn("FSBaseMission.saveSavegame", identity)
        self.assertIn("Mission00.saveSavegame", identity)
        self.assertNotIn("FSCareerMissionInfo.saveToXMLFile =", identity)
        self.assertIn("Mission00", identity)
        self.assertIn("FSBaseMission", identity)
        self.assertIn("Utils.appendedFunction", identity)
        self.assertIn("saveWorldIdentityDuringSave", identity)
        self.assertIn("save hook invoked target=", identity)
        self.assertIn("save hook marker write failed", identity)
        self.assertIn("persisted save-backed marker world=", identity)
        self.assertIn("FS25_SiN_Server_world.xml", identity)
        self.assertIn("missionInfo.savegameDirectory", identity)
        self.assertIn("function(mission)", identity)
        self.assertIn("resolveSaveMissionInfo(mission)", identity)
        self.assertIn("XMLFile.create(\"networkLocalWorldIdentity\"", identity)
        self.assertIn("XMLFile.save returned false", identity)
        self.assertIn("fileExists(path)", identity)
        self.assertIn("careerSavegame.xml", identity)
        self.assertIn('careerSavegame.sinWorldIdentity#worldId', identity)
        self.assertIn('sinWorldIdentity#worldId', identity)
        self.assertIn("waiting for the next FS25 save", source)
        self.assertIn("self.worldIdentityPersisted = false", identity)
        self.assertIn("self.worldIdentityReady = false", identity)
        self.assertIn("self.worldIdentityReady = true", identity)
        self.assertIn("save-backed marker source=", identity)

    def test_world_identity_save_hook_writes_only_after_runtime_identity_exists(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        hook = source[source.index("function FS25SiNServer:saveWorldIdentityDuringSave") :]
        hook = hook[:hook.index("function FS25SiNServer:readSaveWorldIdentity")]
        self.assertLess(hook.index('if self.worldId == nil'), hook.index('XMLFile.create("networkLocalWorldIdentity"'))
        self.assertLess(hook.index('xml:save()'), hook.index('self.worldIdentityReady = true'))
        self.assertIn('self.worldIdentityPersisted = true', hook)
        self.assertIn('self.worldIdentityRetryable = false', hook)

    def test_world_identity_reload_reads_same_marker_and_missing_marker_is_new(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        reader = source[source.index("function FS25SiNServer:readSaveWorldIdentity") :]
        reader = reader[:reader.index("function FS25SiNServer:initializeWorldIdentity")]
        self.assertIn('fileExists(sidecarPath)', reader)
        self.assertIn('return worldId, false, sidecarPath', reader)
        identity = source[source.index("function FS25SiNServer:initializeWorldIdentity") :]
        identity = identity[:identity.index("function FS25SiNServer:shortIdentity")]
        self.assertIn('self.worldIdentityReady = true', identity)
        self.assertIn('initialized new marker=', identity)
        self.assertIn('waiting for the next FS25 save', identity)
        self.assertIn('self.worldIdentityRetryable = false', identity)

    def test_updater_migration_is_complete_and_conflict_safe(self):
        source = (Path(__file__).parents[1] / "scripts" / "Update-SiN.ps1").read_text(encoding="utf-8")
        self.assertIn('[switch]$MigrateLegacyMailbox', source)
        self.assertIn('"FS25_SiN_NetworkLocal"', source)
        self.assertIn('serverBinding.xml', source)
        self.assertIn('Resolve-MailboxUnion', source)
        self.assertIn('Write-MailboxStage', source)
        self.assertIn('FS25_SiN_Server.migration-archive', source)
        self.assertIn('FS25_SiN_NetworkLocal', source)
        self.assertIn('different server bindings', source)
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

    def test_server_runtime_handles_shared_contractor_grant_and_receipt_gated_revocation(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn('requestedRole == "contractor"', source)
        self.assertIn('requestedRole == "revoked"', source)
        self.assertIn("processContractorRevocationCommand", source)
        self.assertIn("revokeAuthorizedContractorState", source)
        self.assertIn("setIsContractingFor", source)
        self.assertIn("getIsContractingFor", source)
        self.assertIn("sourceFarmId", source)
        self.assertIn("refusing to clean legacy contractor permissions from a farm manager", source)

    def test_server_runtime_exposes_read_only_economy_capability_probe(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8"
        )
        self.assertIn('addConsoleCommand("sinEconomy"', source)
        self.assertIn("function FS25SiNServer:consoleCommandEconomy", source)
        probe = source[source.index("function FS25SiNServer:consoleCommandEconomy"):]
        probe = probe[:probe.index("function FS25SiNServer:consumeCommandFile")]
        self.assertIn('observe("getBalance")', probe)
        self.assertIn('observe("getLoan")', probe)
        self.assertIn("read-only probe only", probe)
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

    def test_server_runtime_land_receipt_requires_authoritative_readback_not_client_replication(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        helper = source[source.index("function FS25SiNServer:setAndVerifyLandOwnership") :]
        helper = helper[:helper.index("function FS25SiNServer:processFarmProvisionCommand")]
        self.assertIn("getFarmlandOwner(farmlandId)", helper)
        self.assertIn("owner ~= farmId", helper)
        self.assertIn("pcall(function()", helper)
        self.assertIn("FarmlandStateEvent.new(farmlandId, farmId, 0)", helper)
        self.assertIn("ownership changed but farmland replication event is unavailable", helper)
        self.assertNotIn("changed ~= true", helper)

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
        self.assertIn("function FS25SiNServer:enforceAuthorizedManagerState(user, farm, authorized, syncReason)", source)
        self.assertIn("farm.getUserPermissions", source)
        self.assertIn("farm:setUserPermission(userId, permission, true)", source)
        self.assertIn("function FS25SiNServer:resolvePermissionRecipient(userId, farm)", source)
        self.assertIn("farm.userIdToPlayer[userId]", source)
        self.assertIn("player.connection", source)
        self.assertIn("function FS25SiNServer:replicateFarmPermissions(userId, farm, permissions, manager, farmId, syncReason)", source)
        self.assertIn("PlayerPermissionsEvent.new(userId, permissions or {}, manager == true)", source)
        self.assertIn("pcall(connection.sendEvent, connection, event)", source)
        for diagnostic in ("connectionPresent", "connectionUserId", "delivery=directConnection"):
            self.assertIn(diagnostic, source)
        self.assertNotIn("PlayerPermissionsEvent.sendEvent", source)
        self.assertNotIn("g_server:broadcastEvent(event, nil, nil, player)", source)
        self.assertIn('self:enforceAuthorizedManagerState(user, farm, authorized, "immediate")', source)
        self.assertIn("beforeManager", source)
        self.assertIn("afterManager", source)

    def test_server_runtime_defers_and_validates_manager_client_sync_after_farm_change(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("self.managerSyncDelay = 750", source)
        self.assertIn("function FS25SiNServer:scheduleDeferredManagerSync(user, farm)", source)
        self.assertIn("function FS25SiNServer:processDeferredManagerSyncs()", source)
        self.assertIn("expectedFarmId", source)
        self.assertIn("stale deferred sync discarded", source)
        self.assertIn('user, farm, authorized, "deferred")', source)
        self.assertIn('syncReason == "deferred"', source)
        self.assertIn("final client-sync", source)
        self.assertIn("delivery=directConnection", source)
        self.assertIn("self:processDeferredManagerSyncs()", source)

    def test_server_runtime_periodic_manager_reconciliation_repairs_only_detected_drift(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:reconcileManagerAuthorityDrift()", source)
        self.assertIn("function FS25SiNServer:hasMissingManagerPermissions(farm, permissions)", source)
        self.assertIn("local managerDrift = beforeManager ~= isAuthorized", source)
        self.assertIn("local permissionDrift = isAuthorized and self:hasMissingManagerPermissions(farm, beforePermissions)", source)
        self.assertIn('user, farm, isAuthorized, "periodic-drift")', source)
        self.assertIn("unauthorized manager drift repaired", source)
        self.assertIn("permission drift repaired", source)
        self.assertIn("pcall(self.reconcileManagerAuthorityDrift, self)", source)

    def test_sin_permissions_reports_local_client_state_read_only(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('addConsoleCommand("sinPermissions"', source)
        start = source.index("function FS25SiNServer:consoleCommandPermissions()")
        end = source.index("function FS25SiNServer:update(dt)", start)
        command = source[start:end]
        for field in ("getDiagnosticExecutionSide", "getDiagnosticLocalPlayer", "farmId", "userId",
                      "farmObjectId", "getDiagnosticUniqueUserId", "isUserFarmManager", "getUserPermissions",
                      "permissionCount", "grantedPermissions", "permission "):
            self.assertIn(field, command)
        for forbidden in ("promoteUser", "demoteUser", "setUserPermission", "PlayerPermissionsEvent.sendEvent"):
            self.assertNotIn(forbidden, command)
        self.assertIn("localPlayer=unavailable", command)
        self.assertIn("side=", command)

    def test_sin_self_test_is_registered_read_only_and_counts_map_state(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('addConsoleCommand("sinSelfTest"', source)
        self.assertIn("function FS25SiNServer:consoleCommandSelfTest()", source)
        self.assertIn("SiN Integration Self-Test", source)
        self.assertIn("tableCount(self.activityStates)", source)
        self.assertIn("tableCount(self.deferredManagerSyncs)", source)
        start = source.index("function FS25SiNServer:consoleCommandSelfTest()")
        end = source.index("function FS25SiNServer:update(dt)", start)
        command = source[start:end]
        for mutation in ("promoteUser", "demoteUser", "setUserPermission", "sendEvent", "emitServerEvent"):
            self.assertNotIn(mutation, command)

    def test_read_only_map_probe_uses_verified_runtime_field_sources(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:reportMapProbe()", source)
        for runtime_source in ("getFields", "getId", "getAreaHa", "getCenterOfFieldWorldPosition",
                               "getPolygonPoints", "getWorldTranslation", "polygonBounds",
                               "field.farmland", "getFarmlands", "terrainSize"):
            self.assertIn(runtime_source, source)
        self.assertIn('result("Map probe"', source)
        start = source.index("function FS25SiNServer:reportMapProbe()")
        end = source.index("function FS25SiNServer:consoleCommandSelfTest()", start)
        probe = source[start:end]
        for mutation in ("setTimeScale", "setLandOwnership", "promoteUser", "demoteUser",
                         "setUserPermission", "sendEvent", "emitServerEvent"):
            self.assertNotIn(mutation, probe)

    def test_runtime_map_geometry_export_uses_real_polygons_and_authenticated_event(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:processMapGeometryExport()", source)
        for runtime_source in ("getPolygonPoints", "getWorldTranslation", "field.farmland.id",
                               'event_type", "map_geometry"', "serverEvent.fields.field", "overview_asset_identity",
                               "getFarmlands", "getFarmlandById", "farmlandIds", "serverEvent.farmlands",
                               "#price", "coordinate_system",
                               "source_generation"):
            self.assertIn(runtime_source, source)
        self.assertIn("self.mapGeometryExported", source)

    def test_map_export_handles_keyed_field_manager_tables_and_reports_skips(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:reportMapGeometryExportFailure(reason)", source)
        self.assertIn("for _, field in pairs(fields) do", source)
        self.assertIn("no field polygons were available from FieldManager:getFields", source)
        self.assertNotIn("for _, field in ipairs(fields) do", source)

    def test_permission_mailbox_consumption_removes_exact_command_and_manifest_files(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:consumeCommandFile(operationId)", source)
        self.assertIn("deleteFile(self.commandDirectory", source)
        self.assertIn("deleteFile(manifestPath)", source)

    def test_chat_operation_has_safe_runtime_boundary_and_event_schema(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('operationType == "chat_message"', source)
        self.assertIn('permissionReceipt#status", applied and "applied" or "pending_validation"', source)
        self.assertIn("Mission00.addChatMessage", source)
        self.assertIn('sender = "[Discord] "', source)
        self.assertIn("self.chatInjectionDepth", source)

    def test_position_restore_is_world_scoped_and_on_foot_only(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("FS25_SiN_Server_positions.xml", source)
        self.assertIn("function FS25SiNServer:sampleOnFootPosition", source)
        self.assertIn("function FS25SiNServer:isSafePlayerPosition", source)
        self.assertIn("function FS25SiNServer:restorePendingPlayerPositions", source)
        self.assertIn("mover.setPosition", source)
        self.assertIn("sinPlayerPositions#worldId", source)

    def test_every_normal_lua_receipt_carries_current_world_generation(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("function FS25SiNServer:setReceiptWorldId(receipt, rootKey)", source)
        expected = {
            "processPermissionCommands": ('self:setReceiptWorldId(receipt, "permissionReceipt")', 1),
            "processContractorPermissionCommand": ('self:setReceiptWorldId(receipt, "permissionReceipt")', 1),
            "processContractorRevocationCommand": ('self:setReceiptWorldId(receipt, "permissionReceipt")', 1),
            "processChatCommand": ('self:setReceiptWorldId(receipt, "permissionReceipt")', 1),
            "processFarmProvisionCommand": ('self:setReceiptWorldId(receipt, "networkLocalReceipt")', 1),
            "processNameAlignment": ('self:setReceiptWorldId(receipt, "networkLocalReceipt")', 2),
            "processLandCommand": ('self:setReceiptWorldId(receipt, "networkLocalReceipt")', 1),
        }
        for function_name, (marker, count) in expected.items():
            start = source.index("function FS25SiNServer:" + function_name)
            end = source.find("\nfunction FS25SiNServer:", start + 1)
            block = source[start:] if end == -1 else source[start:end]
            self.assertEqual(block.count(marker), count, function_name)

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

    def test_server_runtime_telemetry_recovers_staged_or_missed_player_tracking(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn("tracker recovered during reconciliation", source)
        self.assertIn("baseline established", source)
        self.assertIn("user.getPlayer", source)
        self.assertIn("playerSystem.getPlayerByUserId", source)
        self.assertIn("player.getCurrentVehicle", source)
        self.assertIn("player.getPosition", source)
        self.assertIn("g_currentMission.getPlayerByUserId", source)
        self.assertIn("self:samplePlayerPosition(record.user_id, record.user)", source)
        self.assertIn("minute completed", source)

    def test_server_runtime_telemetry_tracker_creation_is_idempotent(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        start = source.index("function FS25SiNServer:startActivityTracking")
        end = source.index("function FS25SiNServer:processActivitySamples", start)
        helper = source[start:end]
        self.assertIn("local existing = self.activityStates[uniqueId]", helper)
        self.assertIn("Never reset a live", helper)
        self.assertIn("return false", helper)
        self.assertIn("return true", helper)

    def test_server_self_test_does_not_claim_client_telemetry_tracker_pass(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('result("Telemetry tracker", "UNAVAILABLE", "server-authoritative")', source)
        self.assertIn('executionSide == "server" or executionSide == "listen-server"', source)

    def test_activity_event_uses_existing_mailbox_descriptor(self):
        source = (Path(__file__).parents[1] / "mods" / "FS25_SiN_Server" / "NetworkLocal.lua").read_text(
            encoding="utf-8")
        self.assertIn('self:emitServerEvent("player_activity_minute"', source)
        self.assertIn('eventType ~= "player_activity_minute"', source)
        self.assertIn('self.eventDirectory .. eventId .. ".xml"', source)


class CrossSaveAutoEnrollmentTests(unittest.TestCase):
    SERVER = "sin-fs25-01"
    MAIN_SAVE = "sin-fs25-main"
    HOBO_SAVE = "sin-fs25-hobo"
    UNIQUE_ID = "stable-player"
    DISCORD_ID = "discord-repton"

    def setUp(self):
        self.database = _MemoryDatabase()
        self.auth = AuthorizationManager(self.database)
        self.db = self.database.db

    def seed_approved_main_identity(self):
        self.db.game_identities.insert_one({
            "server_id": self.SERVER, "save_id": self.MAIN_SAVE,
            "discord_id": self.DISCORD_ID, "fs25_unique_user_id": self.UNIQUE_ID,
            "game_player_id": self.UNIQUE_ID, "approved_by": "staff"})
        self.db.community_applications.insert_one({
            "_id": self.DISCORD_ID, "state": "approved", "farm_name": "Repton Does"})

    def test_approved_identity_auto_enrolls_once_on_new_save(self):
        self.seed_approved_main_identity()
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(result["status"], "registered")
        target = self.db.game_identities.find_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "fs25_unique_user_id": self.UNIQUE_ID})
        self.assertEqual(target["discord_id"], self.DISCORD_ID)
        self.assertEqual(target["registration_source"], "approved_cross_save_auto_enrollment")
        self.assertNotIn("approved_by", target)
        for forbidden in ("farm_id", "observation_session", "observation_sequence", "world_id"):
            self.assertNotIn(forbidden, target)
        for collection in ("farm_requests", "memberships", "permission_jobs", "land_operations",
                            "sin_farms", "farm_operations"):
            self.assertEqual(list(getattr(self.db, collection).find({})), [], collection)

    def test_reconnect_and_central_restart_are_idempotent(self):
        self.seed_approved_main_identity()
        first = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        restarted = AuthorizationManager(self.database)
        second = restarted.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(first, second)
        rows = list(self.db.game_identities.find({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "fs25_unique_user_id": self.UNIQUE_ID}))
        self.assertEqual(len(rows), 1)

        processor = CentralEventProcessor(self.database)
        message = processor.activity_message(
            {"server_key": self.SERVER, "display_name": "SiN Test Server 01"}, self.HOBO_SAVE,
            {"event_type": "player_connected", "unique_user_id": self.UNIQUE_ID,
             "display_name": "Repton | Repton Does", "farm_id": 0})
        self.assertNotIn("SiN Registration: Required", message)

    def test_zero_prior_identity_preserves_registration_code_flow(self):
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(result["status"], "registration_required")
        self.assertEqual(len(result["code"]), 8)

    def test_unapproved_prior_identity_does_not_auto_enroll(self):
        self.db.game_identities.insert_one({
            "server_id": self.SERVER, "save_id": self.MAIN_SAVE,
            "discord_id": self.DISCORD_ID, "game_player_id": self.UNIQUE_ID})
        self.db.community_applications.insert_one({
            "_id": self.DISCORD_ID, "state": "pending", "farm_name": "Repton Does"})
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(result["status"], "registration_required")
        self.assertIsNone(self.db.game_identities.find_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "fs25_unique_user_id": self.UNIQUE_ID}))

    def test_unapproved_target_link_still_requires_registration_membership(self):
        self.db.game_identities.insert_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "discord_id": self.DISCORD_ID, "game_player_id": self.UNIQUE_ID,
            "fs25_unique_user_id": self.UNIQUE_ID})
        self.db.community_applications.insert_one({
            "_id": self.DISCORD_ID, "state": "pending", "farm_name": "Repton Does"})
        processor = CentralEventProcessor(self.database)
        message = processor.activity_message(
            {"server_key": self.SERVER, "display_name": "SiN Test Server 01"}, self.HOBO_SAVE,
            {"event_type": "player_connected", "unique_user_id": self.UNIQUE_ID,
             "display_name": "Observed", "farm_id": 0})
        self.assertIn("SiN Registration: Required", message)

    def test_ambiguous_prior_links_fail_closed(self):
        for discord_id in ("discord-a", "discord-b"):
            self.db.game_identities.insert_one({
                "server_id": self.SERVER, "save_id": discord_id,
                "discord_id": discord_id, "game_player_id": self.UNIQUE_ID})
            self.db.community_applications.insert_one({
                "_id": discord_id, "state": "approved", "farm_name": discord_id})
        with self.assertRaisesRegex(ValueError, "ambiguous existing links"):
            self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertIsNone(self.db.game_identities.find_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "fs25_unique_user_id": self.UNIQUE_ID}))

    def test_different_unique_id_requires_registration(self):
        self.seed_approved_main_identity()
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, "different-player")
        self.assertEqual(result["status"], "registration_required")

    def test_identity_on_another_server_does_not_authorize_enrollment(self):
        self.db.game_identities.insert_one({
            "server_id": "another-server", "save_id": self.MAIN_SAVE,
            "discord_id": self.DISCORD_ID, "game_player_id": self.UNIQUE_ID})
        self.db.community_applications.insert_one({
            "_id": self.DISCORD_ID, "state": "approved", "farm_name": "Repton Does"})
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(result["status"], "registration_required")

    def test_target_save_conflict_fails_closed(self):
        self.seed_approved_main_identity()
        self.db.game_identities.insert_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "discord_id": self.DISCORD_ID, "game_player_id": "another-player"})
        with self.assertRaisesRegex(ValueError, "another FS25 identity"):
            self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)

    def test_staff_farm_approval_upgrades_auto_link_without_pregranting_authority(self):
        self.seed_approved_main_identity()
        self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.db.farm_requests.insert_one({
            "_id": "request", "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "discord_id": self.DISCORD_ID, "farm_name": "Repton Does",
            "starting_field": "22", "state": "requested"})
        operation = self.auth.approve_request(
            "request", self.SERVER, self.HOBO_SAVE, 2, self.UNIQUE_ID,
            {"source": "game", "players": {self.UNIQUE_ID: "Repton Does"},
             "farms": {2: "Repton Does"}, "session": "hobo-session", "sequence": 1},
            "staff", True)
        self.assertTrue(operation)
        target = self.db.game_identities.find_one({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE,
            "fs25_unique_user_id": self.UNIQUE_ID})
        self.assertEqual(target["approved_by"], "staff")
        self.assertEqual(list(self.db.memberships.find({})), [])
        self.assertEqual(list(self.db.permission_jobs.find({})), [])

    def test_concurrent_unique_index_winner_is_reused(self):
        self.seed_approved_main_identity()

        class RaceCollection(_MemoryCollection):
            def __init__(self):
                super().__init__()
                self.raced = False

            def insert_one(self, document, **kwargs):
                if not self.raced:
                    self.raced = True
                    self.rows.append(dict(document))
                    raise DuplicateKeyError("concurrent target identity")
                return super().insert_one(document, **kwargs)

        race_collection = RaceCollection()
        race_collection.raced = True
        self.database.db.game_identities = race_collection
        self.db.game_identities.insert_one({
            "server_id": self.SERVER, "save_id": self.MAIN_SAVE,
            "discord_id": self.DISCORD_ID, "fs25_unique_user_id": self.UNIQUE_ID,
            "game_player_id": self.UNIQUE_ID, "approved_by": "staff"})
        race_collection.raced = False
        result = self.auth.registration_request(self.SERVER, self.HOBO_SAVE, self.UNIQUE_ID)
        self.assertEqual(result["status"], "registered")
        self.assertEqual(len(list(self.db.game_identities.find({
            "server_id": self.SERVER, "save_id": self.HOBO_SAVE}))), 1)
