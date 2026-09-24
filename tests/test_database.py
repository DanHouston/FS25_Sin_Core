import os
import inspect
import unittest
from unittest.mock import MagicMock, patch

from fs25_network_core import bot_frontend, server_api
from fs25_network_core.database import Database


class _FakeCollection:
    def __init__(self):
        self.create_calls = []
        self.drop_calls = []
        self.indexes = {}

    def index_information(self):
        return dict(self.indexes)

    def create_index(self, keys, **options):
        self.create_calls.append((keys, options))
        return options.get("name", "index")

    def drop_index(self, index_name):
        self.drop_calls.append(index_name)


class _FakeDatabase:
    name = "fs25_network"

    def __init__(self):
        self.collections = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.collections.setdefault(name, _FakeCollection())


class _FakeAdmin:
    @staticmethod
    def command(name):
        assert name == "hello"
        return {"setName": "rs0"}


class _FakeClient:
    admin = _FakeAdmin()


class DatabaseConfigurationTests(unittest.TestCase):
    def make_database(self, environment):
        client = MagicMock()
        database = MagicMock()
        client.__getitem__.return_value = database
        database.name = environment.get("MONGODB_DATABASE", "fs25_network")
        with patch.dict(os.environ, environment, clear=True), patch("fs25_network_core.database.MongoClient", return_value=client):
            result = Database(uri="mongodb://configured-test")
        return result, client

    def test_default_database_is_fs25_network(self):
        result, client = self.make_database({})
        client.__getitem__.assert_called_once_with("fs25_network")
        self.assertEqual(result.name, "fs25_network")

    def test_environment_database_is_shared_central_selection(self):
        result, client = self.make_database({"MONGODB_DATABASE": "central-production"})
        client.__getitem__.assert_called_once_with("central-production")
        self.assertEqual(result.name, "central-production")

    def test_explicit_name_override_remains_available(self):
        client = MagicMock()
        database = MagicMock(name="legacy-test")
        client.__getitem__.return_value = database
        with patch.dict(os.environ, {"MONGODB_DATABASE": "central-production"}, clear=True), patch("fs25_network_core.database.MongoClient", return_value=client):
            Database(uri="mongodb://configured-test", name="fs25_network_local_test")
        client.__getitem__.assert_called_once_with("fs25_network_local_test")

    def test_central_startups_use_shared_database_resolution(self):
        self.assertIn("database = Database()", inspect.getsource(bot_frontend.main))
        self.assertIn("database = Database()", inspect.getsource(server_api.main))
        self.assertNotIn("Database(name=", inspect.getsource(bot_frontend.main))
        self.assertNotIn("Database(name=", inspect.getsource(server_api.main))

    def test_initialize_does_not_recreate_builtin_id_index_and_is_repeatable(self):
        fake_database = _FakeDatabase()
        database = Database.__new__(Database)
        database.client = _FakeClient()
        database.db = fake_database
        database.name = fake_database.name

        database.initialize()
        database.initialize()

        session_indexes = fake_database.player_activity_sessions.create_calls
        self.assertTrue(session_indexes)
        self.assertTrue(any(
            keys == [("server_key", 1), ("save_key", 1),
                     ("fs25_unique_user_id", 1), ("connected_at", -1)]
            for keys, _options in session_indexes
        ))
        personal_farm_indexes = fake_database.sin_farms.create_calls
        self.assertTrue(any(
            keys == [("server_key", 1), ("save_key", 1),
                     ("world_id", 1), ("owner_discord_id", 1)]
            and options.get("unique") is True
            for keys, options in personal_farm_indexes
        ))
        for collection in fake_database.collections.values():
            for keys, _options in collection.create_calls:
                self.assertNotEqual(keys, "_id")
                self.assertNotEqual(keys, [("_id", 1)])

    def test_initialize_player_activity_sessions_succeeds_without_custom_id_index(self):
        fake_database = _FakeDatabase()
        database = Database.__new__(Database)
        database.client = _FakeClient()
        database.db = fake_database
        database.name = fake_database.name

        database.initialize()

        self.assertEqual(
            [keys for keys, _options in fake_database.player_activity_sessions.create_calls],
            [[("server_key", 1), ("save_key", 1),
              ("fs25_unique_user_id", 1), ("connected_at", -1)],
             [("server_key", 1), ("save_key", 1),
              ("fs25_unique_user_id", 1), ("state", 1)]],
        )

    def test_legacy_event_indexes_are_replaced_only_when_exactly_matching(self):
        collection = MagicMock()
        collection.index_information.return_value = {
            "server_id_1_save_id_1_event_id_1": {
                "key": [("server_id", 1), ("save_id", 1), ("event_id", 1)],
                "unique": True,
            },
            "source_event_id_1": {
                "key": [("source_event_id", 1)],
                "unique": True,
            },
            "unrelated": {
                "key": [("event_id", 1)],
                "unique": True,
            },
        }
        Database._replace_legacy_unique_index(
            collection,
            [("source_event_id", 1)],
            [("server_key", 1), ("save_key", 1), ("source_event_id", 1)],
            name="activity_event_scope", unique=True)
        collection.drop_index.assert_called_once_with("source_event_id_1")
        self.assertNotIn("unrelated", [call.args[0] for call in collection.drop_index.call_args_list])
        collection.create_index.assert_called_once_with(
            [("server_key", 1), ("save_key", 1), ("source_event_id", 1)],
            name="activity_event_scope", unique=True)

    def test_legacy_unique_index_with_partial_filter_is_not_dropped(self):
        collection = MagicMock()
        collection.index_information.return_value = {
            "already_scoped": {
                "key": [("server_id", 1), ("save_id", 1), ("event_id", 1)],
                "unique": True,
                "partialFilterExpression": {"event_id": {"$type": "string"}},
            }
        }
        Database._replace_legacy_unique_index(
            collection,
            [("server_id", 1), ("save_id", 1), ("event_id", 1)],
            [("server_id", 1), ("save_id", 1), ("event_id", 1)],
            name="verified_transfer_event_scope", unique=True,
            partialFilterExpression={"event_id": {"$type": "string"}})
        collection.drop_index.assert_not_called()
