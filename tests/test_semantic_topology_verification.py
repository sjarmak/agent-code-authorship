import subprocess
import tempfile
import unittest
from pathlib import Path

from authorship.semantic_topology_verification import (
    build_candidate_inventory,
    build_matched_control_candidates,
    verify_candidate_inventory,
    verify_citation_with_pinned_git,
)
from authorship.semantic_topology_protocol import canonical_sha256


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class SemanticTopologyVerificationTests(unittest.TestCase):
    def test_deep_search_queries_remain_candidates_not_verified_fields(self):
        artifact = self._run_artifact()

        inventory = build_candidate_inventory(
            [artifact],
            plan_sha256="1" * 64,
            protocol_sha256="2" * 64,
        )

        self.assertEqual(inventory["candidate_count"], 2)
        self.assertEqual(
            {item["candidate_kind"] for item in inventory["candidates"]},
            {"proposed_query", "cited_file"},
        )
        self.assertTrue(
            all(
                item["verification_status"] == "candidate"
                for item in inventory["candidates"]
            )
        )
        self.assertTrue(
            all(
                item["evidence_routes"] == ["deep_search"]
                for item in inventory["candidates"]
            )
        )
        self.assertEqual(canonical_sha256(inventory), inventory["inventory_sha256"])

    def test_candidate_inventory_is_deterministic_and_deduplicates(self):
        artifact = self._run_artifact()

        first = build_candidate_inventory([artifact, artifact], "1" * 64, "2" * 64)
        second = build_candidate_inventory([artifact], "1" * 64, "2" * 64)

        self.assertEqual(first, second)

    def test_pinned_git_verifies_exact_commit_and_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            (repo / "a.py").write_text("answer = 42\n")
            git(repo, "add", "a.py")
            git(repo, "commit", "-qm", "initial")
            commit = git(repo, "rev-parse", "HEAD")
            citation = {
                "repository": "example/repo",
                "path": "a.py",
                "commit": commit,
                "url": "https://example.test/a.py",
            }

            result = verify_citation_with_pinned_git(citation, repo)

        self.assertEqual(result["verification_status"], "verified")
        self.assertEqual(result["evidence_routes"], ["pinned_git"])
        self.assertEqual(result["blob_sha1"], git_blob_sha1("answer = 42\n"))

    def test_missing_blob_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            (repo / "a.py").write_text("answer = 42\n")
            git(repo, "add", "a.py")
            git(repo, "commit", "-qm", "initial")
            commit = git(repo, "rev-parse", "HEAD")

            result = verify_citation_with_pinned_git(
                {
                    "repository": "example/repo",
                    "path": "missing.py",
                    "commit": commit,
                    "url": "https://example.test/missing.py",
                },
                repo,
            )

        self.assertEqual(result["verification_status"], "unavailable")
        self.assertEqual(result["reason"], "blob_not_found_at_pinned_commit")
        self.assertEqual(result["evidence_routes"], [])

    def test_inventory_verification_fails_closed_for_queries_and_missing_repos(self):
        inventory = build_candidate_inventory(
            [self._run_artifact()], "1" * 64, "2" * 64
        )

        result = verify_candidate_inventory(inventory, {}, Path("/missing"))

        statuses = {
            item["candidate_kind"]: (
                item["verification_status"],
                item["reason"],
            )
            for item in result["verifications"]
        }
        self.assertEqual(
            statuses["proposed_query"],
            ("candidate", "query_requires_separate_deterministic_execution"),
        )
        self.assertEqual(
            statuses["cited_file"],
            ("unavailable", "repository_not_in_pinned_pilot_map"),
        )
        self.assertEqual(result["verified_count"], 0)
        self.assertEqual(result["unavailable_count"], 1)
        self.assertEqual(result["candidate_only_count"], 1)

    def test_control_candidates_are_repository_held_out_and_never_labels(self):
        artifact = self._run_artifact()
        artifact["prompt_family_id"] = "semantic_controls"
        artifact["cited_files"].append(
            {
                "repository": "github.com/sg-evals/other-repo",
                "path": "same_shape.py",
                "commit": "6" * 40,
            }
        )

        result = build_matched_control_candidates(
            [artifact],
            sourcegraph_to_canonical={
                "github.com/sg-evals/example-repo": "example/repo",
                "github.com/sg-evals/other-repo": "other/repo",
            },
        )

        self.assertEqual(result["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["source_repository_id"], "example/repo")
        self.assertEqual(candidate["control_repository_id"], "other/repo")
        self.assertTrue(candidate["repository_held_out"])
        self.assertEqual(candidate["status"], "candidate")
        self.assertFalse(candidate["authorship_label_assigned"])
        self.assertEqual(
            result["source_repository_statuses"],
            [
                {
                    "source_repository_id": "example/repo",
                    "run_id": artifact["run_id"],
                    "status": "candidate_generated",
                    "candidate_count": 1,
                    "reason": None,
                }
            ],
        )

    def _run_artifact(self):
        return {
            "run_id": "sha256:" + "3" * 64,
            "artifact_sha256": "4" * 64,
            "canonical_repository_id": "example/repo",
            "prompt_family_id": "adoption_configuration",
            "terminal_status": "completed",
            "proposed_queries": ['repo:^example/repo$ "Agent-Signature"'],
            "cited_files": [
                {
                    "repository": "github.com/sg-evals/example-repo",
                    "path": "AGENTS.md",
                    "commit": "5" * 40,
                    "url": "https://example.test/AGENTS.md",
                }
            ],
            "cited_commits": ["5" * 40],
            "raw_answer": "answer",
        }


def git_blob_sha1(content: str) -> str:
    payload = content.encode()
    import hashlib

    return hashlib.sha1(
        f"blob {len(payload)}\0".encode() + payload, usedforsecurity=False
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
