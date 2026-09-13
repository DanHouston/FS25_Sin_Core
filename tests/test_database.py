import os
import inspect
import unittest
from unittest.mock import MagicMock, patch

from fs25_network_core import bot_frontend, server_api
from fs25_network_core.database import Database


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
