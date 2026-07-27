import unittest
from unittest.mock import patch

from authorship.target_manifest import (
    CUTOFF,
    TargetManifestError,
    build_target_manifest,
    resolve_github_snapshot,
)


class TargetManifestTests(unittest.TestCase):
    def test_build_records_pins_and_unresolved_without_substitution(self):
        def resolver(name, cutoff):
            self.assertEqual(cutoff, CUTOFF)
            if name == "gone/repo":
                raise TargetManifestError("not found")
            return {"commit": "a" * 40, "committed_at": cutoff, "tree": "b" * 40}

        result = build_target_manifest(
            [{"full_name": "ok/repo"}, {"full_name": "gone/repo"}], resolver
        )

        self.assertEqual([row["id"] for row in result["repositories"]], ["ok/repo"])
        self.assertEqual(result["unresolved"], [{"id": "gone/repo", "reason": "not found"}])
        self.assertEqual(result["repositories"][0]["role"], "target")

    def test_unsafe_repository_name_is_rejected_before_process(self):
        with patch("authorship.target_manifest.subprocess.run") as run:
            with self.assertRaisesRegex(TargetManifestError, "unsafe"):
                resolve_github_snapshot("../secrets")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
