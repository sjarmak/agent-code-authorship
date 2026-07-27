import unittest

from authorship.finalize_lineage_adjudication import score_review


def documents(accepted_true: int):
    private_cases = []
    public_cases = []
    for index in range(200):
        case_id = f"{index:064x}"
        selection_class = "accepted" if index < 100 else "rejected"
        private_cases.append(
            {"case_key": case_id, "selection_class": selection_class}
        )
        decision = index < accepted_true if index < 100 else False
        public_cases.append(
            {"case_id": case_id, "is_same_lineage": decision}
        )
    return {"cases": public_cases}, {"cases": private_cases}


class FinalizeLineageAdjudicationTests(unittest.TestCase):
    def test_precision_gate_passes_at_exact_threshold(self):
        public, private = documents(90)

        result = score_review(public, private)

        self.assertEqual(result["structural_precision"], 0.9)
        self.assertTrue(result["structural_match_gate_passed"])

    def test_precision_gate_fails_below_threshold(self):
        public, private = documents(89)

        result = score_review(public, private)

        self.assertFalse(result["structural_match_gate_passed"])

    def test_incomplete_review_remains_pending_without_reveal(self):
        public, private = documents(100)
        public["cases"][0]["is_same_lineage"] = None

        result = score_review(public, private)

        self.assertEqual(result["status"], "pending_blinded_review")
        self.assertNotIn("structural_precision", result)


if __name__ == "__main__":
    unittest.main()
