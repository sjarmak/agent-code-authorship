import json
import unittest
from pathlib import Path

import jsonschema

from authorship.survival_candidates import (
    frame_sha256,
    freeze_candidates,
    validate_candidate_frame,
)


def row(repo, language, agent, pr, sha, additions=250, merged="2025-06-01T00:00:00Z"):
    suffix = ".py" if language == "Python" else ".go"
    return {
        "repository_id": repo,
        "repository_url": f"https://github.com/{repo}",
        "language": language,
        "agent_family": agent,
        "provenance_tier": 2,
        "pr_id": pr,
        "pr_number": pr,
        "pr_url": f"https://github.com/{repo}/pull/{pr}",
        "commit_sha": sha,
        "merged_at": merged,
        "filename": f"src/main{suffix}",
        "additions": additions,
    }


class SurvivalCandidateTests(unittest.TestCase):
    def test_aggregates_exact_commits_and_excludes_overlap(self):
        rows = [
            row("org/a", "Python", "Codex", 1, "a" * 40, 125),
            row("org/a", "Python", "Codex", 1, "b" * 40, 125),
            row("org/target", "Go", "Devin", 2, "c" * 40),
        ]
        frame = freeze_candidates(
            rows, target_ids={"org/target"}, reference_ids=set(), cap=25
        )

        self.assertEqual(len(frame["candidates"]), 1)
        candidate = frame["candidates"][0]
        self.assertEqual(candidate["repository_id"], "org/a")
        self.assertEqual(candidate["commit_shas"], ["a" * 40, "b" * 40])
        self.assertEqual(candidate["pr_numbers"], [1])
        self.assertEqual(candidate["pull_requests"][0]["commit_shas"], ["a" * 40, "b" * 40])
        self.assertEqual(candidate["pull_requests"][0]["attributable_added_lines"], 250)
        self.assertEqual(candidate["attributable_added_lines"], 250)
        self.assertEqual(candidate["default_branch_status"], "pending_git_verification")

    def test_filters_dates_languages_paths_and_minimum_lines(self):
        rows = [
            row("org/old", "Python", "Codex", 1, "a" * 40, 500, "2023-12-31T00:00:00Z"),
            row("org/js", "JavaScript", "Codex", 2, "b" * 40, 500),
            {**row("org/vendor", "Go", "Codex", 3, "c" * 40, 500), "filename": "vendor/x.go"},
            {**row("org/missing", "Go", "Codex", 6, "f" * 40, 500), "filename": None},
            row("org/tiny", "Go", "Codex", 4, "d" * 40, 199),
            row("org/good", "Go", "Codex", 5, "e" * 40, 200),
        ]
        frame = freeze_candidates(rows, target_ids=set(), reference_ids=set(), cap=25)

        self.assertEqual([c["repository_id"] for c in frame["candidates"]], ["org/good"])

    def test_cap_is_deterministic_within_stratum(self):
        rows = [
            row(f"org/repo-{i}", "Python", "Codex", i, f"{i:040x}", 200)
            for i in range(30)
        ]
        first = freeze_candidates(rows, target_ids=set(), reference_ids=set(), cap=25)
        second = freeze_candidates(reversed(rows), target_ids=set(), reference_ids=set(), cap=25)

        self.assertEqual(first, second)
        self.assertEqual(len(first["candidates"]), 25)

    def test_tier_one_is_never_removed_by_stratum_cap(self):
        rows = [
            {
                **row(f"org/tier1-{i}", "Python", "Codex", i, f"{i:040x}", 200),
                "provenance_tier": 1,
            }
            for i in range(27)
        ]
        frame = freeze_candidates(rows, target_ids=set(), reference_ids=set(), cap=25)

        self.assertEqual(len(frame["candidates"]), 27)

    def test_multi_agent_tier_two_repository_is_excluded(self):
        rows = [
            row("org/mixed", "Python", "Codex", 1, "a" * 40, 250),
            row("org/mixed", "Python", "Cursor", 2, "b" * 40, 250),
            row("org/single", "Python", "Codex", 3, "c" * 40, 250),
        ]

        frame = freeze_candidates(rows, target_ids=set(), reference_ids=set(), cap=25)

        self.assertEqual(
            [candidate["repository_id"] for candidate in frame["candidates"]],
            ["org/single"],
        )
        self.assertEqual(frame["exclusions"]["multi_agent_tier_2_repositories"], ["org/mixed"])

    def test_validator_rejects_digest_and_repository_leakage(self):
        candidate = freeze_candidates(
            [row("org/one", "Go", "Codex", 1, "a" * 40, 250)],
            target_ids=set(),
            reference_ids=set(),
            cap=25,
        )["candidates"][0]
        candidate["default_branch_status"] = "github_verified"
        frame = {"outcomes_consulted": False, "candidates": [candidate, dict(candidate)]}
        frame["frame_sha256"] = frame_sha256(frame)

        errors = validate_candidate_frame(frame)

        self.assertIn("repository groups must be unique", errors)
        frame["frame_sha256"] = "0" * 64
        self.assertIn(
            "frame_sha256 does not match canonical frame",
            validate_candidate_frame(frame),
        )

    def test_frozen_manifest_validates(self):
        root = Path(__file__).resolve().parents[1]
        frame = json.loads((root / "study" / "survival-candidates.v1.json").read_text())
        schema = json.loads(
            (root / "study" / "survival-candidates.schema.json").read_text()
        )

        self.assertEqual(validate_candidate_frame(frame), [])
        jsonschema.Draft202012Validator(schema).validate(frame)


if __name__ == "__main__":
    unittest.main()
