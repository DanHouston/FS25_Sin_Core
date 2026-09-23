import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fs25_network_core.modpack import ModpackError, ModpackManager, ModpackPaths


class ModpackDistributionTests(unittest.TestCase):
    server = {"server_key": "sin-fs25-01", "display_name": "SiN Test Server 01"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.publication = root / "publication"
        self.client = root / "client"
        self.source.mkdir()
        self.manager = ModpackManager(ModpackPaths(self.source, self.publication, self.client))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _mod(path: Path, marker: str) -> bytes:
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("modDesc.xml", f"<modDesc>{marker}</modDesc>")
            archive.writestr("content.txt", marker)
        return path.read_bytes()

    def test_capture_requires_explicit_approved_mods_and_preserves_bytes(self):
        first = self._mod(self.source / "FS25_SiN_First.zip", "first")
        second = self._mod(self.source / "FS25_SiN_ThirdParty.zip", "third-party")
        self._mod(self.source / "NotApproved.zip", "not-approved")

        with self.assertRaisesRegex(ModpackError, "explicit approved mod"):
            self.manager.capture_approved(self.server, "2026.09.22", None)
        approved = self.manager.capture_approved(
            self.server, "2026.09.22", ["FS25_SiN_ThirdParty.zip", "FS25_SiN_First.zip"])
        release = approved.release_dir
        self.assertFalse((self.publication / self.server["server_key"] / "Current").exists())
        self.assertEqual((release / "mods" / "FS25_SiN_First.zip").read_bytes(), first)
        self.assertEqual((release / "mods" / "FS25_SiN_ThirdParty.zip").read_bytes(), second)
        self.assertFalse((release / "mods" / "NotApproved.zip").exists())
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([mod["filename"] for mod in manifest["mods"]],
                         ["FS25_SiN_First.zip", "FS25_SiN_ThirdParty.zip"])
        for mod in manifest["mods"]:
            raw = (release / "mods" / mod["filename"]).read_bytes()
            self.assertEqual(mod["size"], len(raw))
            self.assertEqual(mod["sha256"], hashlib.sha256(raw).hexdigest())
        with ZipFile(release / manifest["modpack_filename"]) as archive:
            self.assertEqual(set(archive.namelist()), {
                "mods/FS25_SiN_First.zip", "mods/FS25_SiN_ThirdParty.zip"})
            self.assertEqual(archive.read("mods/FS25_SiN_First.zip"), first)

    def test_capture_is_deterministic_and_identical_reapproval_is_idempotent(self):
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        first = self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        first_manifest = (first.release_dir / "manifest.json").read_bytes()
        first_pack = (first.release_dir / first.manifest["modpack_filename"]).read_bytes()
        second = self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        self.assertEqual(first.release_dir, second.release_dir)
        self.assertEqual(first_manifest, (second.release_dir / "manifest.json").read_bytes())
        self.assertEqual(first_pack, (second.release_dir / second.manifest["modpack_filename"]).read_bytes())

    def test_publish_validates_before_current_swap_and_restart_reload_is_safe(self):
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        approved = self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        current = self.manager.publish_approved(self.server, "r1")
        self.assertEqual(current.manifest, approved.manifest)
        reloaded = ModpackManager(ModpackPaths(self.source, self.publication, self.client))
        self.assertEqual(reloaded.validate_current(self.server).manifest, approved.manifest)
        release_mod = approved.release_dir / "mods" / "FS25_SiN_First.zip"
        release_mod.write_bytes(b"tampered")
        with self.assertRaisesRegex(ModpackError, "hash|ZIP|manifest"):
            reloaded.publish_approved(self.server, "r1")
        self.assertEqual(reloaded.validate_current(self.server).manifest, approved.manifest)

    def test_refresh_publish_reuses_only_current_approved_set_and_updates_changed_bytes(self):
        first = self._mod(self.source / "FS25_SiN_First.zip", "first")
        self._mod(self.source / "ThirdParty.zip", "third")
        approved = self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        self.manager.publish_approved(self.server, "r1")

        replacement = self._mod(self.source / "FS25_SiN_First.zip", "replacement")
        refreshed = self.manager.refresh_and_publish(self.server, "r2")
        self.assertEqual(refreshed.modpack_version, "r2")
        self.assertEqual(refreshed.manifest["mods"][0]["filename"], "FS25_SiN_First.zip")
        self.assertEqual(refreshed.manifest["mods"][0]["sha256"], hashlib.sha256(replacement).hexdigest())
        self.assertNotEqual(refreshed.manifest["mods"][0]["sha256"], hashlib.sha256(first).hexdigest())
        self.assertEqual(self.manager.validate_current(self.server).manifest, refreshed.manifest)
        self.assertFalse((refreshed.release_dir / "mods" / "ThirdParty.zip").exists())

    def test_refresh_publish_fails_closed_if_an_approved_source_mod_is_missing(self):
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        self.manager.publish_approved(self.server, "r1")
        (self.source / "FS25_SiN_First.zip").unlink()
        with self.assertRaisesRegex(ModpackError, "approved mod source is missing"):
            self.manager.refresh_and_publish(self.server, "r2")
        self.assertEqual(self.manager.validate_current(self.server).modpack_version, "r1")

    def test_invalid_current_is_not_silently_overwritten(self):
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        approved = self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        current = self.manager.publish_approved(self.server, "r1")
        (current.release_dir / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ModpackError):
            self.manager.publish_approved(self.server, "r1")
        self.assertEqual((current.release_dir / "manifest.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual(approved.manifest["modpack_version"], "r1")

    def test_default_sync_preserves_unmanaged_and_strict_mode_removes_it(self):
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        self.manager.capture_approved(self.server, "r1", ["FS25_SiN_First.zip"])
        self.manager.publish_approved(self.server, "r1")
        self.client.mkdir()
        unrelated = self._mod(self.client / "LocalOnly.zip", "local")
        default = self.manager.sync_client(self.server)
        self.assertEqual(default["removed"], [])
        self.assertEqual((self.client / "LocalOnly.zip").read_bytes(), unrelated)
        self.assertTrue((self.client / "FS25_SiN_First.zip").is_file())
        strict = self.manager.sync_client(self.server, strict=True)
        self.assertEqual(strict["removed"], ["LocalOnly.zip"])
        self.assertFalse((self.client / "LocalOnly.zip").exists())

    def test_paths_are_configurable_without_operator_directories(self):
        paths = ModpackPaths.from_environment({
            "SIN_MODPACK_SOURCE_DIR": "source",
            "SIN_MODPACK_PUBLICATION_ROOT": "drive",
            "SIN_MODPACK_CLIENT_MOD_DIR": "client",
        })
        self.assertEqual(paths.source_dir, Path("source"))
        self.assertEqual(paths.publication_root, Path("drive"))
        self.assertEqual(paths.client_mod_dir, Path("client"))
        with self.assertRaisesRegex(ModpackError, "SIN_MODPACK_SOURCE_DIR"):
            ModpackPaths.from_environment({"SIN_MODPACK_PUBLICATION_ROOT": "drive"})

    def test_bad_source_and_bad_zip_fail_closed(self):
        with self.assertRaisesRegex(ModpackError, "source directory"):
            ModpackManager(ModpackPaths(self.source / "missing", self.publication)).capture_approved(
                self.server, "r1", ["missing.zip"])
        (self.source / "bad.zip").write_bytes(b"not a zip")
        with self.assertRaisesRegex(ModpackError, "valid ZIP"):
            self.manager.capture_approved(self.server, "r1", ["bad.zip"])

    def test_publication_file_destination_fails_without_replacing_anything(self):
        destination = self.publication / "not-a-directory"
        self.publication.mkdir(exist_ok=True)
        destination.write_text("destination blocker", encoding="utf-8")
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        with self.assertRaisesRegex(ModpackError, "publication root"):
            ModpackManager(ModpackPaths(self.source, destination)).capture_approved(
                self.server, "r1", ["FS25_SiN_First.zip"])

    def test_publication_cannot_be_nested_in_read_only_source(self):
        nested = self.source / "published"
        self._mod(self.source / "FS25_SiN_First.zip", "first")
        with self.assertRaisesRegex(ModpackError, "source directory"):
            ModpackManager(ModpackPaths(self.source, nested)).capture_approved(
                self.server, "r1", ["FS25_SiN_First.zip"])


if __name__ == "__main__":
    unittest.main()
