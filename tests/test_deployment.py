import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.build_release import build


class DeploymentPackagingTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def _run_migration(self, mailbox_root):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is not installed on this runner")
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(self.root / "scripts" / "Update-SiN.ps1"), "-MigrationOnly",
             "-MailboxDir", str(mailbox_root)],
            capture_output=True, text=True, check=False,
        )

    def test_release_contains_client_updater_and_valid_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            output_path = Path(folder)
            (output_path / "FS25_SiN_NetworkLocal.zip").write_bytes(b"legacy artifact")
            output = build("v0.1.8-rc", output_path)
            manifest = json.loads((output / "build-manifest.json").read_text(encoding="utf-8"))
            updater = output / "Update-SiN.ps1"
            client = output / "Update-SiN-Client.ps1"
            self.assertTrue((output / "sin-agent.zip").is_file())
            self.assertTrue((output / "FS25_SiN_Server.zip").is_file())
            self.assertTrue((output / "SHA256SUMS.txt").is_file())
            self.assertTrue(updater.is_file())
            self.assertTrue(client.is_file())
            self.assertEqual(hashlib.sha256(updater.read_bytes()).hexdigest(), manifest["updater_sha256"])
            self.assertEqual(hashlib.sha256(client.read_bytes()).hexdigest(), manifest["client_updater_sha256"])
            sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn("sin-agent.zip", sums)
            self.assertIn("FS25_SiN_Server.zip", sums)
            self.assertIn("Update-SiN.ps1", sums)
            self.assertIn("Update-SiN-Client.ps1", sums)
            checksum_entries = {
                line.split(None, 1)[1]: line.split(None, 1)[0]
                for line in sums.splitlines() if line.strip()
            }
            for asset in ("sin-agent.zip", "FS25_SiN_Server.zip", "Update-SiN.ps1", "Update-SiN-Client.ps1"):
                self.assertEqual(hashlib.sha256((output / asset).read_bytes()).hexdigest(),
                                 checksum_entries[asset])
            with ZipFile(output / "FS25_SiN_Server.zip") as archive:
                self.assertIn("modDesc.xml", archive.namelist())
                self.assertIn("NetworkLocal.lua", archive.namelist())
                self.assertIn("events/SiNRegistrationWarningEvent.lua", archive.namelist())
            with ZipFile(output / "sin-agent.zip") as archive:
                agent_source = archive.read("fs25_network_core/agent.py").decode("utf-8")
                self.assertIn("process_events_once(self.event_batch_size)", agent_source)
                self.assertIn("_event_minute_sequence", agent_source)
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
            result = self._run_migration(new)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((old / "serverBinding.xml").read_text(encoding="utf-8"), "old")
            self.assertEqual((new / "serverBinding.xml").read_text(encoding="utf-8"), "new")

    def test_updater_consolidates_the_split_same_binding_live_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_a = root / "FS25SiNNetworkLocal"
            old_b = root / "FS25_SiN_NetworkLocal"
            new = root / "FS25_SiN_Server"
            binding = b'<serverBinding serverKey="opaque" credential="not-printed"/>'
            for base in (old_a, old_b):
                (base / "permission-commands").mkdir(parents=True)
                (base / "registration-responses").mkdir()
                (base / "serverBinding.xml").write_bytes(binding)
                (base / "manager-authority.xml").write_text('<managerAuthority schemaVersion="1"/>', encoding="utf-8")
                (base / "permission-commands" / "same-command.xml").write_text("same", encoding="utf-8")
            (old_a / "snapshot.xml").write_text('<networkLocal sequence="1"/>', encoding="utf-8")
            (old_b / "snapshot.xml").write_text('<networkLocal sequence="2"/>', encoding="utf-8")
            (old_a / "clock-policy.xml").write_text('<clockPolicy generated="old"/>', encoding="utf-8")
            (old_b / "clock-policy.xml").write_text('<clockPolicy generated="new"/>', encoding="utf-8")
            (old_a / "permission-commands" / "server-pairing-response.xml").write_text("pairing", encoding="utf-8")
            (old_b / "permission-receipts").mkdir()
            (old_b / "permission-receipts" / "receipt.xml.failed").write_text("failed", encoding="utf-8")
            (old_a / "registration-responses" / "a.xml").write_text("a", encoding="utf-8")
            (old_b / "registration-responses" / "b.xml").write_text("b", encoding="utf-8")
            older = (old_a / "snapshot.xml").stat().st_mtime - 120
            os.utime(old_a / "snapshot.xml", (older, older))
            os.utime(old_a / "clock-policy.xml", (older, older))
            result = self._run_migration(new)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(old_a.exists())
            self.assertFalse(old_b.exists())
            self.assertEqual((new / "serverBinding.xml").read_bytes(), binding)
            self.assertEqual((new / "snapshot.xml").read_text(encoding="utf-8"), '<networkLocal sequence="2"/>')
            self.assertEqual((new / "clock-policy.xml").read_text(encoding="utf-8"), '<clockPolicy generated="new"/>')
            self.assertTrue((new / "permission-commands" / "server-pairing-response.xml").is_file())
            self.assertTrue((new / "permission-receipts" / "receipt.xml.failed").is_file())
            self.assertTrue((new / "registration-responses" / "a.xml").is_file())
            self.assertTrue((new / "registration-responses" / "b.xml").is_file())
            self.assertEqual(len(list((root / "FS25_SiN_Server.migration-archive").iterdir())), 1)
            self.assertNotIn("not-printed", result.stdout + result.stderr)

    def test_updater_rejects_two_legacy_bindings_that_differ(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_a = root / "FS25SiNNetworkLocal"
            old_b = root / "FS25_SiN_NetworkLocal"
            for base, key in ((old_a, "a"), (old_b, "b")):
                base.mkdir()
                (base / "serverBinding.xml").write_text(
                    '<serverBinding serverKey="%s" credential="secret"/>' % key, encoding="utf-8")
            new = root / "FS25_SiN_Server"
            result = self._run_migration(new)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(old_a.exists())
            self.assertTrue(old_b.exists())
            self.assertFalse(new.exists())
            self.assertNotIn("secret", result.stdout + result.stderr)

    def test_updater_rejects_conflicting_unknown_or_durable_state(self):
        for relative_path in ("unknown.dat", "manager-authority.xml", "permission-commands\\same.xml"):
            with self.subTest(relative_path=relative_path), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                old_a = root / "FS25SiNNetworkLocal"
                old_b = root / "FS25_SiN_NetworkLocal"
                binding = '<serverBinding serverKey="same" credential="secret"/>'
                for base, value in ((old_a, "a"), (old_b, "b")):
                    base.mkdir()
                    (base / "serverBinding.xml").write_text(binding, encoding="utf-8")
                    (base / relative_path).parent.mkdir(parents=True, exist_ok=True)
                    (base / relative_path).write_text(value, encoding="utf-8")
                result = self._run_migration(root / "FS25_SiN_Server")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(old_a.exists() and old_b.exists())
                self.assertNotIn("secret", result.stdout + result.stderr)

    def test_updater_handles_empty_legacy_roots_and_interrupted_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_empty_a = root / "FS25SiNNetworkLocal"
            old_empty_b = root / "FS25_SiN_NetworkLocal"
            old_empty_a.mkdir()
            old_empty_b.mkdir()
            new = root / "FS25_SiN_Server"
            result = self._run_migration(new)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(new.is_dir())
            self.assertFalse(old_empty_a.exists())
            self.assertFalse(old_empty_b.exists())

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / "FS25_SiN_Server.migrating"
            stage.mkdir()
            (stage / "serverBinding.xml").write_text('<serverBinding serverKey="same" credential="secret"/>', encoding="utf-8")
            new = root / "FS25_SiN_Server"
            result = self._run_migration(new)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((new / "serverBinding.xml").read_text(encoding="utf-8"),
                             '<serverBinding serverKey="same" credential="secret"/>')
            self.assertFalse(stage.exists())
            self.assertNotIn("secret", result.stdout + result.stderr)

    def test_updater_ignores_and_archives_empty_legacy_skeleton_when_canonical_is_populated(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is not installed on this runner")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            new = root / "FS25_SiN_Server"
            binding = b'<serverBinding serverKey="opaque" credential="not-printed"/>'
            new.mkdir()
            (new / "serverBinding.xml").write_bytes(binding)
            (new / "events").mkdir()
            old = root / "FS25_SiN_NetworkLocal"
            (old / "events").mkdir(parents=True)
            (old / "permission-commands").mkdir()
            (old / "registration-requests").mkdir()

            result = self._run_migration(new)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((new / "serverBinding.xml").read_bytes(), binding)
            self.assertFalse(old.exists())
            archive_root = root / "FS25_SiN_Server.migration-archive"
            archived = list(archive_root.rglob("FS25_SiN_NetworkLocal"))
            self.assertEqual(len(archived), 1)
            self.assertNotIn("not-printed", result.stdout + result.stderr)

    def test_updater_keeps_agent_stopped_if_mailbox_migration_fails(self):
        source = (self.root / "scripts" / "Update-SiN.ps1").read_text(encoding="utf-8")
        stop_before_migration = source.index("    Stop-Agent\n    # Migration")
        migration_start = source.index("    Invoke-LegacyMailboxMigration -Destination $MailboxDir", stop_before_migration)
        start_after_migration = source.index("    Start-Agent", migration_start)
        self.assertLess(stop_before_migration, migration_start)
        self.assertGreater(start_after_migration, migration_start)

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
