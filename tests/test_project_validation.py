from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "validate_project.py"
MODULE_SPEC = importlib.util.spec_from_file_location("validate_project", SCRIPT_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
validate_project = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(validate_project)


class ProjectValidationTest(unittest.TestCase):
    def test_current_project_contracts_pass(self) -> None:
        self.assertEqual(validate_project.validate_project(PROJECT_ROOT), [])

    def test_eval_validation_rejects_duplicate_ids_and_weak_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "evals.json"
            eval_cases = []
            for case_number in range(10):
                eval_cases.append(
                    {
                        "id": 1 if case_number == 9 else case_number + 1,
                        "prompt": f"prompt {case_number}",
                        "expected_output": "expected",
                        "files": [],
                        "assertions": ["only one"],
                        "tags": ["contract"],
                    }
                )
            target.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "skill_name": "skillnull",
                        "evals": eval_cases,
                    }
                ),
                encoding="utf-8",
            )
            errors = validate_project.validate_eval_file(target)
            self.assertTrue(any("duplicated" in error for error in errors))
            self.assertTrue(any("two assertions" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
