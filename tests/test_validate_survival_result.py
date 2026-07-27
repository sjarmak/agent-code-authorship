import copy
import json
import unittest
from pathlib import Path

from authorship.validate_survival_result import validate


class ValidateSurvivalResultTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(
            Path("results/survival-study-result.v1.json").read_text()
        )
        self.report = Path("results/SURVIVAL_REPORT.v1.md").read_text()

    def test_current_result_validates(self):
        self.assertEqual(validate(self.document, self.report), [])

    def test_pending_result_rejects_structural_headline(self):
        document = copy.deepcopy(self.document)
        document["status"] = "pending_blinded_review"
        document["unresolved_blinded_cases"] = 1
        document["lineage_validation"]["status"] = "pending_blinded_review"
        document["lineage_validation"]["structural_match_gate_passed"] = False
        document["structural_matches_in_headline"] = True

        self.assertIn(
            "pending structural matches cannot enter headline",
            validate(document, self.report),
        )

    def test_final_result_requires_complete_validation(self):
        document = copy.deepcopy(self.document)
        document["lineage_validation"]["status"] = "pending_blinded_review"
        document["structural_matches_in_headline"] = False

        self.assertIn(
            "final status requires complete lineage validation",
            validate(document, self.report),
        )

    def test_structural_headline_rejects_contradictory_report(self):
        report = (
            self.report
            + "\n- Structural matches do not enter these headline results.\n"
        )

        self.assertIn(
            "report contradicts structural headline handling",
            validate(self.document, report),
        )


if __name__ == "__main__":
    unittest.main()
