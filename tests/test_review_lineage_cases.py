import unittest

from authorship.review_lineage_cases import apply_decision, progress


class ReviewLineageCasesTests(unittest.TestCase):
    def test_decisions_and_progress(self):
        document = {
            "cases": [
                {"is_same_lineage": None},
                {"is_same_lineage": None},
            ]
        }

        self.assertTrue(apply_decision(document["cases"][0], "yes"))
        self.assertTrue(document["cases"][0]["is_same_lineage"])
        self.assertEqual(progress(document), (1, 2))
        self.assertFalse(apply_decision(document["cases"][1], "skip"))


if __name__ == "__main__":
    unittest.main()
