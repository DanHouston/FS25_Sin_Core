import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.build_release import build


class DeploymentPackagingTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def test_release_contains_client_updater_and_valid_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            output = build("v0.1.8-rc", Path(folder))
            manifest = json.loads((output / "build-manifest.json").read_text(encoding="utf-8"))
            client = output / "Update-SiN-Client.ps1"
            self.assertTrue(client.is_file())
            self.assertEqual(hashlib.sha256(client.read_bytes()).hexdigest(), manifest["client_updater_sha256"])
            sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn("Update-SiN-Client.ps1", sums)
            with ZipFile(output / "FS25_SiN_NetworkLocal.zip") as archive:
                self.assertIn("modDesc.xml", archive.namelist())
                self.assertIn("NetworkLocal.lua", archive.namelist())

    def test_updater_defaults_to_canonical_mailbox_and_client_updater_is_secret_free(self):
        updater = (self.root / "scripts" / "Update-SiN.ps1").read_text(encoding="utf-8")
        client = (self.root / "scripts" / "Update-SiN-Client.ps1").read_text(encoding="utf-8")
        self.assertIn("FS25_SiN_NetworkLocal", updater)
        self.assertIn("MigrateLegacyMailbox", updater)
        self.assertIn("SHA256SUMS.txt", client)
        self.assertNotIn("serverBinding.xml", client)
        self.assertNotIn("MONGODB", client)

