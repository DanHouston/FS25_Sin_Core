import os
import tempfile
import time
import unittest
from pathlib import Path

from fs25_network_core.local_test import read_snapshot, simulate


class LocalTestTests(unittest.TestCase):
    def test_simulation_round_trip_is_explicitly_simulated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.xml"
            result = simulate(path)
            self.assertEqual(result["source"], "simulator")
            self.assertEqual(result["farms"], {1: "Local Test Farm"})
            with self.assertRaises(FileExistsError):
                simulate(path)

    def test_stale_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.xml"
            simulate(path)
            old = time.time() - 60
            os.utime(path, (old, old))
            with self.assertRaisesRegex(ValueError, "stale"):
                read_snapshot(path)

    def test_duplicate_farms_and_wrong_schema_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.xml"
            simulate(path)
            original = path.read_text()
            node = '<farm farmId="1" name="Duplicate" />'
            path.write_text(original.replace("</farms>", node + "</farms>"))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_snapshot(path)
            path.write_text(original.replace('schemaVersion="1"', 'schemaVersion="99"'))
            with self.assertRaisesRegex(ValueError, "schema"):
                read_snapshot(path)

    def test_invalid_farm_diagnostics_distinguish_causes(self):
        cases = [
            ('farmId="1"', "farmId 1 is missing its name attribute"),
            ('farmId="255" name="Farm"', "outside the supported range"),
            ('name="Farm"', "missing or non-integer farmId"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.xml"
            for attributes, message in cases:
                with self.subTest(attributes=attributes):
                    path.write_text('<networkLocal schemaVersion="1" source="game" session="test" sequence="1" savegameIndex="1">'
                                    f'<farms><farm {attributes}/></farms></networkLocal>')
                    with self.assertRaisesRegex(ValueError, message):
                        read_snapshot(path)

    def test_game_snapshot_with_unnamed_farm_preserves_both_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.xml"
            path.write_text('''<?xml version="1.0" encoding="utf-8" standalone="no"?>
<networkLocal schemaVersion="1" source="game" session="20260909223829" sequence="33" savegameIndex="1">
    <farms>
        <farm farmId="1" name="My farm"/>
        <farm farmId="14" name=""/>
    </farms>
</networkLocal>''', encoding="utf-8")
            snapshot = read_snapshot(path)
            self.assertEqual(snapshot["farms"], {1: "My farm", 14: ""})
            self.assertEqual(snapshot["unnamed_farm_ids"], [14])
            self.assertEqual(snapshot["source"], "game")
