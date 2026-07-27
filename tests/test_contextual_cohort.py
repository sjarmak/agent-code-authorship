import unittest

from authorship.contextual_cohort import match_targets, parse_numstat_history


class ContextualCohortTests(unittest.TestCase):
    def test_numstat_filters_language_and_vendored_paths(self):
        payload = """__COMMIT__aaa\t2025-01-01T00:00:00Z
10\t2\tapp.py
20\t0\tvendor/generated.py
30\t0\tmain.go
"""
        self.assertEqual(
            parse_numstat_history(payload, "Python"),
            [
                {
                    "commit": "aaa",
                    "committed_at": "2025-01-01T00:00:00Z",
                    "added_lines": 10,
                }
            ],
        )

    def test_matching_excludes_attributed_commits_and_does_not_reuse(self):
        targets = [
            {
                "repository_id": "o/r",
                "source_commit": "source-a",
                "pr_number": 1,
                "landing_at": "2025-01-01T00:00:00Z",
                "line_count": 100,
            },
            {
                "repository_id": "o/r",
                "source_commit": "source-b",
                "pr_number": 2,
                "landing_at": "2025-01-02T00:00:00Z",
                "line_count": 100,
            },
        ]
        candidates = [
            {
                "commit": "excluded",
                "committed_at": "2025-01-01T00:00:00Z",
                "added_lines": 100,
            },
            {
                "commit": "context-a",
                "committed_at": "2025-01-03T00:00:00Z",
                "added_lines": 100,
            },
            {
                "commit": "context-b",
                "committed_at": "2025-01-04T00:00:00Z",
                "added_lines": 100,
            },
        ]

        first = match_targets(
            targets, candidates, excluded_commits={"excluded"}
        )
        second = match_targets(
            list(reversed(targets)), candidates, excluded_commits={"excluded"}
        )

        self.assertEqual(first, second)
        selected = [
            match["context_commit"]
            for match in first
            if match["status"] == "matched"
        ]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertNotIn("excluded", selected)

    def test_unmatched_target_is_explicit(self):
        target = {
            "repository_id": "o/r",
            "source_commit": "source",
            "pr_number": 1,
            "landing_at": "2025-01-01T00:00:00Z",
            "line_count": 100,
        }
        result = match_targets(
            [target],
            [
                {
                    "commit": "too-late",
                    "committed_at": "2026-01-01T00:00:00Z",
                    "added_lines": 100,
                }
            ],
            excluded_commits=set(),
        )

        self.assertEqual(result[0]["status"], "unmatched")


if __name__ == "__main__":
    unittest.main()
