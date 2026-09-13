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
