import unittest

from fs25_network_core.integration_campaign import _MemoryDatabase
from fs25_network_core.server_registry import ServerRegistry


class ActiveRuntimeTests(unittest.TestCase):
    SERVER = "sin-fs25-01"

    def setUp(self):
        self.database = _MemoryDatabase()
        self.db = self.database.db
        self.db.sin_servers.insert_one({"_id": self.SERVER, "server_key": self.SERVER,
                                        "display_name": "SiN Test Server", "enabled": True,
                                        "credential_hash": "paired"})
        self.db.sin_saves.insert_one({"_id": f"{self.SERVER}:sin-fs25-main",
                                      "server_key": self.SERVER, "save_key": "sin-fs25-main",
                                      "fs25_save_id": "1"})
        self.db.sin_saves.insert_one({"_id": f"{self.SERVER}:sin-fs25-hobo",
                                      "server_key": self.SERVER, "save_key": "sin-fs25-hobo",
                                      "fs25_save_id": "3"})
        self.registry = ServerRegistry(self.database)

    @staticmethod
    def snapshot(save_id, world_id, runtime_generation, sequence=1, session="session"):
        return {"source": "game", "savegame_index": save_id, "world_id": world_id,
                "runtime_generation": runtime_generation, "sequence": sequence,
                "session": session}

    def test_delayed_old_save_cannot_switch_active_runtime_but_new_runtime_can(self):
        hobo = self.snapshot("3", "hobo-world", 20, session="hobo-session")
        self.registry.activate_runtime(self.SERVER, "sin-fs25-hobo", hobo)

        with self.assertRaisesRegex(ValueError, "stale FS25 runtime"):
            self.registry.activate_runtime(self.SERVER, "sin-fs25-main",
                                           self.snapshot("1", "courtright-world", 19,
                                                         session="courtright-old"))
        self.assertEqual(self.registry.active_runtime(self.SERVER)["save_key"], "sin-fs25-hobo")

        courtright = self.snapshot("1", "courtright-world", 21, session="courtright-new")
        self.registry.activate_runtime(self.SERVER, "sin-fs25-main", courtright)
        self.assertEqual(self.registry.active_runtime(self.SERVER)["save_key"], "sin-fs25-main")

    def test_active_runtime_survives_registry_restart(self):
        self.registry.activate_runtime(self.SERVER, "sin-fs25-hobo",
                                       self.snapshot("3", "hobo-world", 20, session="hobo"))
        restarted = ServerRegistry(self.database)
        self.assertEqual(restarted.active_runtime(self.SERVER)["world_id"], "hobo-world")
        with self.assertRaisesRegex(ValueError, "stale FS25 runtime"):
            restarted.validate_runtime_snapshot(
                self.SERVER, "sin-fs25-main",
                self.snapshot("1", "courtright-world", 19, session="courtright"))

    def test_discord_discovery_exposes_only_active_save_for_normal_purposes(self):
        self.registry.activate_runtime(self.SERVER, "sin-fs25-hobo",
                                       self.snapshot("3", "hobo-world", 20, session="hobo"))
        records = self.registry.eligible_servers("reconcile")
        self.assertEqual(records[0]["active_save_key"], "sin-fs25-hobo")
        self.assertEqual([row["save_key"] for row in records[0]["saves"]], ["sin-fs25-hobo"])
        info = self.registry.eligible_servers("info")
        self.assertEqual({row["save_key"] for row in info[0]["saves"]},
                         {"sin-fs25-main", "sin-fs25-hobo"})

    def test_legacy_runtime_cannot_switch_saves_without_runtime_generation(self):
        self.registry.activate_runtime(self.SERVER, "sin-fs25-main",
                                       self.snapshot("1", "courtright-world", 0, session="old"))
        with self.assertRaisesRegex(ValueError, "runtime generation evidence"):
            self.registry.validate_runtime_snapshot(
                self.SERVER, "sin-fs25-hobo",
                self.snapshot("3", "hobo-world", 0, session="new"))

    def test_mapping_remap_requires_expected_old_id(self):
        row = self.registry.configure_save(self.SERVER, "sin-fs25-main", "3",
                                            expected_fs25_save_id="1")
        self.assertEqual(row["fs25_save_id"], "3")
        with self.assertRaisesRegex(ValueError, "did not match"):
            self.registry.configure_save(self.SERVER, "sin-fs25-main", "1",
                                         expected_fs25_save_id="1")


if __name__ == "__main__":
    unittest.main()
