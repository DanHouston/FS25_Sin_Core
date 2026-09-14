import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.build_release import build


class DeploymentPackagingTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def test_release_contains_client_updater_and_valid_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            output_path = Path(folder)
            (output_path / "FS25_SiN_NetworkLocal.zip").write_bytes(b"legacy artifact")
            output = build("v0.1.8-rc", output_path)
            manifest = json.loads((output / "build-manifest.json").read_text(encoding="utf-8"))
            client = output / "Update-SiN-Client.ps1"
            self.assertTrue(client.is_file())
            self.assertEqual(hashlib.sha256(client.read_bytes()).hexdigest(), manifest["client_updater_sha256"])
            sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn("Update-SiN-Client.ps1", sums)
            with ZipFile(output / "FS25_SiN_Server.zip") as archive:
                self.assertIn("modDesc.xml", archive.namelist())
                self.assertIn("NetworkLocal.lua", archive.namelist())
                self.assertIn("events/SiNRegistrationWarningEvent.lua", archive.namelist())
            self.assertFalse((output / "FS25_SiN_NetworkLocal.zip").exists())
            self.assertEqual(
                hashlib.sha256((output / "FS25_SiN_Server.zip").read_bytes()).hexdigest(),
                manifest["server_sha256"],
            )

    def test_updater_migrates_complete_legacy_mailbox_without_rewriting_binding(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is not installed on this runner")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "FS25_SiN_NetworkLocal"
            new = root / "FS25_SiN_Server"
            binding = b'<?xml version="1.0"?><serverBinding serverKey="opaque" credential="not-printed"/>'
            for directory in (
                "events", "permission-commands", "permission-receipts",
                "registration-requests", "registration-responses",
            ):
                (old / directory).mkdir(parents=True)
            (old / "serverBinding.xml").write_bytes(binding)
            (old / "snapshot.xml").write_text("snapshot", encoding="utf-8")
            (old / "manager-authority.xml").write_text("authority", encoding="utf-8")
            (old / "clock-policy.xml").write_text("clock", encoding="utf-8")
            (old / "events" / "event.xml").write_text("pending", encoding="utf-8")
            (old / "permission-commands" / "command.xml").write_text("pending", encoding="utf-8")
            (old / "permission-receipts" / "receipt.xml").write_text("pending", encoding="utf-8")
            (old / "registration-requests" / "request.xml").write_text("pending", encoding="utf-8")
            (old / "registration-responses" / "response.xml").write_text("pending", encoding="utf-8")
            result = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(self.root / "scripts" / "Update-SiN.ps1"), "-MigrationOnly",
                 "-MailboxDir", str(new)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(old.exists())
            self.assertEqual((new / "serverBinding.xml").read_bytes(), binding)
            self.assertTrue((new / "events" / "event.xml").is_file())
            self.assertTrue((new / "permission-commands" / "command.xml").is_file())
            self.assertTrue((new / "permission-receipts" / "receipt.xml").is_file())
            self.assertTrue((new / "registration-requests" / "request.xml").is_file())
            self.assertTrue((new / "registration-responses" / "response.xml").is_file())
            second = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(self.root / "scripts" / "Update-SiN.ps1"), "-MigrationOnly",
                 "-MailboxDir", str(new)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((new / "serverBinding.xml").read_bytes(), binding)

    def test_updater_refuses_nonempty_legacy_and_canonical_mailbox_conflict(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is not installed on this runner")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "FS25_SiN_NetworkLocal"
            new = root / "FS25_SiN_Server"
            old.mkdir()
            new.mkdir()
            (old / "serverBinding.xml").write_text("old", encoding="utf-8")
            (new / "serverBinding.xml").write_text("new", encoding="utf-8")
            result = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(self.root / "scripts" / "Update-SiN.ps1"), "-MigrationOnly",
                 "-MailboxDir", str(new)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((old / "serverBinding.xml").read_text(encoding="utf-8"), "old")
            self.assertEqual((new / "serverBinding.xml").read_text(encoding="utf-8"), "new")

    def test_updater_defaults_to_canonical_mailbox_and_client_updater_is_secret_free(self):
        updater = (self.root / "scripts" / "Update-SiN.ps1").read_text(encoding="utf-8")
        client = (self.root / "scripts" / "Update-SiN-Client.ps1").read_text(encoding="utf-8")
        self.assertIn('modSettings\\FS25_SiN_Server', updater)
        self.assertIn('$env:SIN_MAILBOX_DIR = $MailboxDir', updater)
        self.assertIn("FS25_SiN_Server", updater)
        self.assertIn("FS25_SiN_NetworkLocal", updater)
        self.assertIn("MigrateLegacyMailbox", updater)
        self.assertIn("SHA256SUMS.txt", client)
        self.assertNotIn("serverBinding.xml", client)
        self.assertNotIn("MONGODB", client)
