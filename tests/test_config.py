import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fs25_network_core.config import load_local_environment, required_setting


class ConfigTests(unittest.TestCase):
    def test_private_files_load_without_interpolating_secrets(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            (root / "atlas-credentials.env").write_text(
                'MONGODB_URI="mongodb+srv://user:literal${PASSWORD}@example.invalid/"\n', encoding="utf-8-sig")
            (root / ".env").write_text("DISCORD_TOKEN=test-token\nMONGODB_URI=must-not-override\n", encoding="utf-8")
            load_local_environment(root)
            self.assertEqual(required_setting("DISCORD_TOKEN"), "test-token")
            self.assertEqual(required_setting("MONGODB_URI"), "mongodb+srv://user:literal${PASSWORD}@example.invalid/")

    def test_process_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MONGODB_URI": "explicit"}, clear=True):
            root = Path(folder)
            (root / "atlas-credentials.env").write_text("MONGODB_URI=file-value\n", encoding="utf-8")
            load_local_environment(root)
            self.assertEqual(required_setting("MONGODB_URI"), "explicit")

    def test_missing_settings_do_not_fall_back_to_localhost(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            load_local_environment(Path(folder))
            with self.assertRaisesRegex(ValueError, "Missing MONGODB_URI"):
                required_setting("MONGODB_URI")
