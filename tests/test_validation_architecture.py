import unittest
from pathlib import Path

from scripts.validate_repository import validate_repository


class ValidationArchitectureTests(unittest.TestCase):
    def test_fast_static_gate_validates_source_imports_lua_and_config(self):
        result = validate_repository(import_modules=True)
        self.assertGreaterEqual(result["python_files"], 60)
        self.assertGreaterEqual(result["imported_modules"], 25)
        self.assertEqual(result["lua_files"], 3)

    def test_ci_campaign_report_stays_in_ignored_ephemeral_output(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "--output local-test/integration-campaign.json",
            workflow,
        )
        self.assertIn(
            'Path("local-test/integration-campaign.json").read_text()',
            workflow,
        )
        self.assertNotIn(
            "--output integration-campaign.json",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
