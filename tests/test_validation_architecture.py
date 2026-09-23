import unittest

from scripts.validate_repository import validate_repository


class ValidationArchitectureTests(unittest.TestCase):
    def test_fast_static_gate_validates_source_imports_lua_and_config(self):
        result = validate_repository(import_modules=True)
        self.assertGreaterEqual(result["python_files"], 60)
        self.assertGreaterEqual(result["imported_modules"], 25)
        self.assertEqual(result["lua_files"], 2)


if __name__ == "__main__":
    unittest.main()

