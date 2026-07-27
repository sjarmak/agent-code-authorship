import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authorship.pin_survival_git import pin
from authorship.survival_git import SurvivalGitError


class PinSurvivalGitTests(unittest.TestCase):
    def test_retrieval_failure_is_recorded_not_raised(self):
        candidate = {
            "repository_id": "org/missing",
            "repository_url": "https://github.com/org/missing",
            "default_branch": "main",
            "pull_requests": [{"merge_commit": "a" * 40}],
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "authorship.pin_survival_git.clone_or_fetch",
            side_effect=SurvivalGitError("not found"),
        ):
            result = pin(candidate, Path(directory) / "repos", Path(directory) / "bundles")

        self.assertEqual(result["status"], "retrieval_failed")
        self.assertEqual(result["error"], "not found")


if __name__ == "__main__":
    unittest.main()
