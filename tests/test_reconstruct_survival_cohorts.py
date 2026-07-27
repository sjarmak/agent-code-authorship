import unittest

from authorship.reconstruct_survival_cohorts import deduplicate_physical_lines


def record(pr_number: int):
    return {
        "repository_id": "o/r",
        "merge_commit": "a" * 40,
        "path": "app.py",
        "line_number": 3,
        "content_sha256": "b" * 64,
        "pr_number": pr_number,
        "pr_url": f"https://example.test/{pr_number}",
    }


class ReconstructSurvivalCohortTests(unittest.TestCase):
    def test_duplicate_physical_line_keeps_lowest_pr_attribution(self):
        retained, excluded = deduplicate_physical_lines([record(332), record(331)])

        self.assertEqual([item["pr_number"] for item in retained], [331])
        self.assertEqual(excluded[0]["excluded_pr_number"], 332)
        self.assertEqual(
            excluded[0]["reason"], "duplicate_physical_line_attribution"
        )


if __name__ == "__main__":
    unittest.main()
