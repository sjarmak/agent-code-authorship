import unittest

from authorship.github_reachability import (
    apply_reachability,
    classify_repository,
    graphql_query,
)


class GitHubReachabilityTests(unittest.TestCase):
    def test_query_requests_default_branch_and_pr_facts(self):
        query = graphql_query("owner", "repo", [12, 34])

        self.assertIn('repository(owner:"owner",name:"repo")', query)
        self.assertIn("p0:pullRequest(number:12)", query)
        self.assertIn("p1:pullRequest(number:34)", query)
        self.assertIn("baseRefName", query)
        self.assertIn("mergeCommit{oid}", query)

    def test_classifies_only_merged_prs_on_default_branch_before_cutoff(self):
        batches = [
            {
                "defaultBranchRef": {"name": "main"},
                "p0": {
                    "number": 1,
                    "baseRefName": "main",
                    "merged": True,
                    "mergedAt": "2025-01-01T00:00:00Z",
                    "mergeCommit": {"oid": "a" * 40},
                },
                "p1": {
                    "number": 2,
                    "baseRefName": "dev",
                    "merged": True,
                    "mergedAt": "2025-01-01T00:00:00Z",
                    "mergeCommit": {"oid": "b" * 40},
                },
            }
        ]

        result = classify_repository("org/repo", [1, 2], batches)

        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(result["eligible_pr_numbers"], [1])
        self.assertEqual(result["eligible_prs"][0]["merge_commit"], "a" * 40)
        self.assertEqual(result["ineligible_prs"]["base_branch_mismatch"], [2])

    def test_applying_facts_recomputes_candidate_from_eligible_prs(self):
        frame = {
            "candidates": [
                {
                    "repository_id": "org/repo",
                    "pull_requests": [
                        {
                            "id": 10,
                            "number": 1,
                            "url": "u1",
                            "merged_at": "2025-01-01T00:00:00Z",
                            "commit_shas": ["a" * 40],
                            "attributable_added_lines": 210,
                        },
                        {
                            "id": 20,
                            "number": 2,
                            "url": "u2",
                            "merged_at": "2025-02-01T00:00:00Z",
                            "commit_shas": ["b" * 40],
                            "attributable_added_lines": 500,
                        },
                    ],
                }
            ]
        }
        facts = {
            "repositories": [
                {
                    "repository_id": "org/repo",
                    "default_branch": "main",
                    "eligible_pr_numbers": [1],
                    "eligible_prs": [
                        {
                            "number": 1,
                            "merge_commit": "c" * 40,
                            "merged_at": "2025-01-01T00:00:00Z",
                            "base_ref": "main",
                        }
                    ],
                }
            ]
        }

        result = apply_reachability(frame, facts)

        candidate = result["candidates"][0]
        self.assertEqual(candidate["pr_numbers"], [1])
        self.assertEqual(candidate["commit_shas"], ["a" * 40])
        self.assertEqual(candidate["pull_requests"][0]["merge_commit"], "c" * 40)
        self.assertEqual(candidate["attributable_added_lines"], 210)
        self.assertEqual(candidate["default_branch_status"], "github_verified")


if __name__ == "__main__":
    unittest.main()
