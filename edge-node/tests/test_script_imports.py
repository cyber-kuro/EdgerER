import importlib
import unittest


class ScriptImportTests(unittest.TestCase):
    def test_existing_offline_and_live_scripts_import_without_hardware(self):
        modules = (
            "scripts.record_ppg_csv",
            "scripts.analyze_ppg_csv",
            "scripts.estimate_rr_from_ppg_csv",
            "scripts.validate_rr_recordings",
            "scripts.test_live_rr_packet",
            "scripts.rr_validation_session",
        )
        for module_name in modules:
            with self.subTest(module=module_name):
                importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
