import unittest

from authorship.contextual_comparison import build_comparison, paired_estimate


class ContextualComparisonTests(unittest.TestCase):
    def test_paired_bootstrap_is_deterministic(self):
        pairs = [
            (
                {"unchanged": 8, "modified": 0, "deleted": 2},
                {"unchanged": 7, "modified": 0, "deleted": 3},
            ),
            (
                {"unchanged": 6, "modified": 0, "deleted": 4},
                {"unchanged": 9, "modified": 0, "deleted": 1},
            ),
        ]

        self.assertEqual(
            paired_estimate(pairs, replicates=20, seed=7),
            paired_estimate(pairs, replicates=20, seed=7),
        )

    def test_comparison_requires_identical_repository_pairs(self):
        row = {
            "repository_id": "o/r",
            "counts": {
                str(horizon): {"unchanged": 10}
                for horizon in (30, 90, 180, 365)
            },
        }
        with self.assertRaises(ValueError):
            build_comparison(
                {"repositories": [row]},
                {"repositories": []},
                {"candidates": [{"repository_id": "o/r", "language": "Python"}]},
                replicates=5,
                seed=1,
            )


if __name__ == "__main__":
    unittest.main()
