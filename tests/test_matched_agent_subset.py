import unittest

from authorship.matched_agent_subset import matched_target_keys


class MatchedAgentSubsetTests(unittest.TestCase):
    def test_only_successfully_matched_targets_are_selected(self):
        audit = {
            "targets": [
                {
                    "source_commit": "a",
                    "pr_number": 1,
                    "status": "matched",
                },
                {
                    "source_commit": "b",
                    "pr_number": 2,
                    "status": "unmatched",
                },
            ]
        }

        self.assertEqual(matched_target_keys(audit), {("a", 1)})


if __name__ == "__main__":
    unittest.main()
