"""Unmodified evaluation modules remain byte-compatible with the baseline."""
import hashlib
import unittest
from pathlib import Path


class EvaluationPortTests(unittest.TestCase):
    def test_five_unmodified_evaluation_core_modules_are_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        original = root.parent / "YanXi_project" / "src" / "evaluation"
        # app.py/presentation.py implement the categorized UI. agents.py now
        # remembers rejected generated cases so Qwen cannot repeat one batch
        # until retries expire; scoring thresholds remain covered separately.
        names = ("__init__.py", "models.py", "reports.py", "service.py", "settings.py")
        if not all((original / name).is_file() for name in names):
            self.skipTest("Original sibling project not available; no external files are required to run the new project")
        copied = root / "src" / "evaluation"
        for name in names:
            with self.subTest(module=name):
                self.assertTrue((copied / name).is_file())
                self.assertEqual(hashlib.sha256((copied / name).read_bytes()).hexdigest(),
                    hashlib.sha256((original / name).read_bytes()).hexdigest(),
                    "Evaluation rules/service/reports must stay byte-for-byte compatible")


if __name__ == "__main__":
    unittest.main()
