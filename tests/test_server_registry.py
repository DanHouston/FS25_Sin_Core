import unittest
from unittest.mock import MagicMock

from fs25_network_core.server_registry import ServerRegistry


class ServerRegistryPairingTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.record = {"_id": "local-dev", "server_key": "local-dev",
                       "enabled": True, "credential_hash": None,
                       "pairing_code_hash": "hashed-code"}
        self.db.sin_servers.find_one.return_value = self.record
        self.db.sin_servers.update_one.return_value.modified_count = 1
        self.registry = ServerRegistry(MagicMock(db=self.db))

    def test_pair_code_returns_key_and_plaintext_but_updates_only_hash(self):
        server_key, credential = self.registry.pair_code(" abc123 ")
        self.assertEqual(server_key, "local-dev")
        self.assertTrue(credential)
        update = self.db.sin_servers.update_one.call_args.args[1]
        self.assertNotIn(credential, repr(update))
        self.assertNotEqual(update["$set"]["credential_hash"], credential)
        self.assertIn("$unset", update)

    def test_missing_code_covers_expired_and_already_used_rejection(self):
        self.db.sin_servers.find_one.return_value = None
        for code in ("expired", "already-used"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.registry.pair_code(code)
