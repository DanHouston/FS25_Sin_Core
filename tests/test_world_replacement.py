import unittest

from fs25_network_core.activity_telemetry import ActivitySessionProcessor, ActivityTelemetryProcessor
from fs25_network_core.banking_engine import BankingEngine
from fs25_network_core.business_workflows import ContractService
from fs25_network_core.integration_campaign import _MemoryDatabase
from fs25_network_core.map_service import MapService, MapStore
from fs25_network_core.farm_lifecycle import FarmLifecycle
from fs25_network_core.world_generation import WorldGenerationRegistry

from tests.test_map_service import synthetic_model


class WorldReplacementScenarioTests(unittest.TestCase):
    SERVER = "server-a"
    SAVE = "save-a"

    @staticmethod
    def snapshot(world_id, map_id, farms, farmlands):
        return {
            "source": "game", "world_id": world_id, "map_id": map_id,
            "savegame_index": 1, "farms": farms, "players": {},
            "farmlands": farmlands,
        }

    def setUp(self):
        self.database = _MemoryDatabase()
        self.lifecycle = FarmLifecycle(self.database)
        self.worlds = WorldGenerationRegistry(self.database)
        self.db = self.database.db

    def test_activity_replacement_does_not_reuse_old_current_projection(self):
        self.lifecycle.record_snapshot(self.SERVER, self.SAVE, self.snapshot(
            "generation-a", "courtright", {"1": "SiN Harvest", "2": "Repton Does"}, {"22": 2}))
        telemetry = ActivityTelemetryProcessor(self.database)
        sessions = ActivitySessionProcessor(self.database)
        old = {"unique_user_id": "stable-player", "user_id": "7", "farm_id": "2",
               "display_name": "Repton", "session_id": "session-a", "world_id": "generation-a"}
        sessions.connected(self.SERVER, self.SAVE, "connect-a", old)
        telemetry.process(self.SERVER, self.SAVE, "minute-a", dict(old, minute_sequence=1,
            activity_bucket="active", inactive_minutes=0))

        self.lifecycle.record_snapshot(self.SERVER, self.SAVE, self.snapshot(
            "generation-b", "hobo", {"1": "SiN Harvest", "2": "Unrelated Farm"}, {"22": 0}))
        current = sessions.connected(self.SERVER, self.SAVE, "connect-b", {
            "unique_user_id": "stable-player", "user_id": "9", "farm_id": "2",
            "display_name": "New Player", "session_id": "session-b", "world_id": "generation-b"})

        old_rows = list(self.db.player_activity_sessions.find({"world_id": "generation-a"}))
        status = telemetry.status(self.SERVER, self.SAVE, "stable-player", "generation-b")
        self.assertEqual(len(old_rows), 1)
        self.assertEqual(old_rows[0]["observed_display_name"], "Repton")
        self.assertEqual(status["sessions"][0]["session_id"], "session-b")
        self.assertEqual(status["sessions"][0]["observed_farm_id"], "2")
        self.assertEqual(status["sessions"][0]["observed_display_name"], "New Player")
        self.assertEqual(current["session_id"], "session-b")
        self.assertFalse(any(row.get("session_id") == "session-a" for row in status["sessions"]))
        self.assertEqual(self.db.player_activity_sessions.find_one({
            "world_id": "generation-a", "session_id": "session-a"})["world_generation_state"], "historical")

    def test_composed_world_replacement_isolated_and_survives_reload(self):
        # Generation A: all world-local identities intentionally overlap the
        # values used again by the replacement save.
        self.lifecycle.record_snapshot(self.SERVER, self.SAVE, self.snapshot(
            "generation-a", "courtright", {"1": "SiN Harvest", "2": "Repton Does"}, {"22": 2}))
        telemetry = ActivityTelemetryProcessor(self.database)
        sessions = ActivitySessionProcessor(self.database)
        player = {"unique_user_id": "stable-player", "user_id": "7", "farm_id": "2",
                  "display_name": "Repton", "session_id": "session-a", "world_id": "generation-a"}
        sessions.connected(self.SERVER, self.SAVE, "connect-a", player)
        telemetry.process(self.SERVER, self.SAVE, "minute-a", dict(player, minute_sequence=1,
            activity_bucket="active", inactive_minutes=0))

        store = MapStore(self.database)
        model_a = synthetic_model()
        store.persist(self.SERVER, self.SAVE, model_a, source_generation=4, world_id="generation-a")
        contracts = ContractService(self.database)
        contract = contracts.create("repton-member", "Courtright Field 1", "Harvest Field 1", 500,
                                    server_key=self.SERVER, save_key=self.SAVE, fields="1",
                                    server_name="Courtright")
        self.db.memberships.insert_one({"_id": "manager-a", "server_id": self.SERVER,
            "save_id": self.SAVE, "world_id": "generation-a", "farm_id": 2,
            "discord_id": "repton-member", "desired_role": "farm_manager", "state": "active",
            "applied_role": "farm_manager"})
        self.db.farm_operations.insert_one({"_id": "authority-a", "operation_id": "authority-a",
            "server_key": self.SERVER, "save_key": self.SAVE, "world_id": "generation-a",
            "operation_type": "assign_farmland", "farm_id": 2, "farmland_id": 22, "state": "pending"})
        self.db.deposit_requests.insert_one({"_id": "deposit-a", "deposit_id": "deposit-a",
            "server_id": self.SERVER, "save_id": self.SAVE, "world_id": "generation-a",
            "discord_id": "repton-member", "farm_id": 2, "amount": 50,
            "operation_id": "bridge-a", "state": "pending"})
        self.db.farm_operations.insert_one({"_id": "bridge-a", "operation_id": "bridge-a",
            "server_key": self.SERVER, "save_key": self.SAVE, "world_id": "generation-a",
            "operation_type": "deposit_funds", "farm_id": 2, "state": "pending"})

        # Replacement world has the same configured endpoint and overlapping
        # numeric IDs, but every current lookup must resolve only this state.
        self.lifecycle.record_snapshot(self.SERVER, self.SAVE, self.snapshot(
            "generation-b", "hobo", {"1": "SiN Harvest", "2": "Unrelated Farm"}, {"22": 0}))
        model_b_payload = model_a.to_dict()
        model_b_payload["map_title"] = "Hobo's Hollow"
        model_b_payload["fields"] = {"1": {**model_b_payload["fields"]["22"], "field_id": 1}}
        model_b_payload["farmlands"] = {"22": {**model_b_payload["farmlands"]["1"], "farmland_id": 22}}
        model_b = type(model_a).from_dict(model_b_payload)
        store.persist(self.SERVER, self.SAVE, model_b, source_generation=1, world_id="generation-b")
        service = MapService()
        self.assertTrue(service.load_persisted(store, self.SERVER, self.SAVE, "generation-b"))
        self.assertEqual(service.model(self.SERVER, self.SAVE, "generation-b").map_title, "Hobo's Hollow")
        self.assertEqual(self.lifecycle.latest_snapshot(self.SERVER, self.SAVE)["map_id"], "hobo")
        self.assertEqual(self.lifecycle.latest_snapshot(self.SERVER, self.SAVE)["farmlands"], {"22": 0})

        self.assertEqual(contracts.open(self.SERVER, self.SAVE), [])
        with self.assertRaisesRegex(ValueError, "world generation"):
            contracts.accept(contract["contract_id"], "unrelated-member", farm_id=2)
        with self.assertRaisesRegex(ValueError, "world generation"):
            BankingEngine(self.database).settle_deposit("deposit-a", "applied", {
                "operation_id": "bridge-a", "source_event_id": "game-a", "receipt": "old"},
                self.SERVER, self.SAVE, "generation-b")
        self.assertEqual(self.db.farm_operations.find_one({"_id": "authority-a"})["state"], "world_superseded")
        self.assertEqual(self.db.farm_operations.find_one({"_id": "bridge-a"})["state"], "world_superseded")
        self.assertEqual(self.db.memberships.find_one({"_id": "manager-a"})["world_generation_state"], "historical")
        self.assertEqual(self.db.deposit_requests.find_one({"_id": "deposit-a"})["world_generation_state"], "historical")
        self.assertEqual(self.db.contracts.find_one({"contract_id": contract["contract_id"]})["world_generation_state"], "historical")

        # A process restart/reload must not fall back to the Courtright map.
        restarted = MapService()
        self.assertTrue(restarted.load_persisted(store, self.SERVER, self.SAVE, "generation-b"))
        self.assertEqual(restarted.model(self.SERVER, self.SAVE, "generation-b").map_title, "Hobo's Hollow")
        self.assertTrue(restarted.render_contract_map(self.SERVER, self.SAVE, "1", "generation-b"))
        with self.assertRaises(Exception):
            restarted.model(self.SERVER, self.SAVE, "generation-a")


if __name__ == "__main__":
    unittest.main()
