import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema

from authorship.semantic_change_materialization import (
    extract_hunk_records,
    materialize_pilot_records,
)
from authorship.semantic_topology_protocol import validate_semantic_change_record


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class SemanticChangeMaterializationTests(unittest.TestCase):
    def test_hunks_have_full_identity_and_deterministic_followup_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "agent@example.com")
            git(repo, "config", "user.name", "Introducing Author")
            (repo / "module.py").write_text("value = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "module.py").write_text(
                "value = 1\n\n\ndef calculate(number):\n    return number + 1\n"
            )
            git(repo, "commit", "-qam", "introduce function")
            introducing = git(repo, "rev-parse", "HEAD")
            git(repo, "config", "user.email", "reviewer@example.com")
            git(repo, "config", "user.name", "Followup Author")
            (repo / "module.py").write_text(
                "value = 1\n\n\ndef calculate(number):\n    return number + 2\n"
            )
            git(repo, "commit", "-qam", "fix behavior")
            cutoff = git(repo, "rev-parse", "HEAD")

            records = extract_hunk_records(
                repo=repo,
                repository_id="example/repo",
                sourcegraph_name="github.com/sg-evals/example-repo",
                introducing_commit=introducing,
                diff_base=base,
                cutoff_commit=cutoff,
                provenance={
                    "class": "attributable_agent",
                    "source_artifact": "study/example.json",
                    "source_sha256": "1" * 64,
                    "outcomes_consulted": False,
                },
            )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(validate_semantic_change_record(record), [])
        self.assertEqual(record["identity"]["introducing_commit"], introducing)
        self.assertEqual(record["identity"]["diff_base"], base)
        self.assertEqual(record["identity"]["cutoff_commit"], cutoff)
        self.assertEqual(record["tests_and_docs"]["path_class"], "source")
        self.assertEqual(record["subsequent_changes"]["commit_count"], 1)
        self.assertEqual(
            record["human_assimilation"]["distinct_followup_author_count"], 1
        )
        self.assertTrue(record["human_assimilation"]["first_followup_author_differs"])
        self.assertFalse(record["human_assimilation"]["human_identity_inferred"])
        self.assertTrue(record["observability"]["cutoff_reachable"])

    def test_test_and_documentation_paths_are_classified_without_authorship_inference(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "a@example.com")
            git(repo, "config", "user.name", "A")
            (repo / "README.md").write_text("base\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "test_feature.py").write_text(
                "def test_feature():\n    assert True\n"
            )
            (repo / "README.md").write_text("base\nnew docs\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "tests and docs")
            introducing = git(repo, "rev-parse", "HEAD")

            records = extract_hunk_records(
                repo,
                "example/repo",
                "github.com/sg-evals/example-repo",
                introducing,
                base,
                introducing,
                {
                    "class": "attributable_agent",
                    "source_artifact": "study/example.json",
                    "source_sha256": "1" * 64,
                    "outcomes_consulted": False,
                },
            )

        self.assertEqual(
            {record["tests_and_docs"]["path_class"] for record in records},
            {"test", "documentation"},
        )

    def test_missing_commit_is_inventory_failure_not_empty_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "plan_sha256": "1" * 64,
                "protocol_sha256": "2" * 64,
                "repositories": [
                    {
                        "canonical_repository_id": "missing/repo",
                        "sourcegraph_name": "github.com/sg-evals/missing-repo",
                        "cutoff_commit": "3" * 40,
                        "stratum": "attributable_agent",
                        "source_artifact": "study/events.json",
                        "source_sha256": "4" * 64,
                        "introduction_event": {
                            "commit_shas": ["5" * 40],
                            "agent_family": "Agent",
                        },
                    }
                ],
            }

            inventory = materialize_pilot_records(
                plan,
                repository_root=root,
                output_root=root / "output",
            )

        self.assertEqual(inventory["record_count"], 0)
        self.assertEqual(inventory["failure_count"], 1)
        self.assertEqual(inventory["failures"][0]["reason"], "repository_unavailable")

    def test_record_schema_accepts_materialized_record(self):
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "study/semantic-change-record.schema.json"
            ).read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "a@example.com")
            git(repo, "config", "user.name", "A")
            (repo / "a.go").write_text("package a\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "a.go").write_text("package a\n\nfunc A() {}\n")
            git(repo, "commit", "-qam", "add")
            head = git(repo, "rev-parse", "HEAD")
            record = extract_hunk_records(
                repo,
                "example/repo",
                "github.com/sg-evals/example-repo",
                head,
                base,
                head,
                {
                    "class": "attributable_agent",
                    "source_artifact": "study/example.json",
                    "source_sha256": "1" * 64,
                    "outcomes_consulted": False,
                },
            )[0]

        jsonschema.Draft202012Validator(schema).validate(record)


if __name__ == "__main__":
    unittest.main()
