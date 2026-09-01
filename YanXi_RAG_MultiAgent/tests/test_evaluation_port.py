"""Presentation may evolve; the six original evaluation-core files stay intact."""
import hashlib
import unittest
from pathlib import Path


class EvaluationPortTests(unittest.TestCase):
    def test_six_evaluation_core_modules_are_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        original = root.parent / "YanXi_project" / "src" / "evaluation"
        # User explicitly requested clearer result tabs. app.py and the new
        # presentation.py are UI-only changes, not a change to judging rules.
        names = ("__init__.py", "agents.py", "models.py", "reports.py", "service.py", "settings.py")
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
