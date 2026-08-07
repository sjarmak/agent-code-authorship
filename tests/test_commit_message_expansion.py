import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema

from authorship.commit_message_expansion import (
    ExpansionAnalysisError,
    build_expansion_analysis,
    main,
)


ROOT = Path(__file__).resolve().parents[1]


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CommitMessageExpansionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "example__accepted"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "Test Author")
        git(self.repository, "config", "user.email", "test@example.com")
        for index in range(30):
            path = self.repository / "module.py"
            path.write_text(f"value = {index}\n")
            git(self.repository, "add", "module.py")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "commit",
                    "-q",
                    "-m",
                    f"fix: pre adoption {index}",
                ],
                check=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "GIT_AUTHOR_DATE": f"2024-01-{index + 1:02d}T00:00:00Z",
                    "GIT_COMMITTER_DATE": f"2024-01-{index + 1:02d}T00:00:00Z",
                },
            )
        path.write_text("value = 31\n")
        git(self.repository, "add", "module.py")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "commit",
                "-q",
                "-m",
                "feat: post adoption",
            ],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": "2024-02-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-02-01T00:00:00Z",
            },
        )
        self.cutoff = git(self.repository, "rev-parse", "HEAD")
        self.event = self.cutoff

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adjudication(self, pre_count: int = 30) -> Path:
        path = self.root / "adjudication.json"
        path.write_text(
            json.dumps(
                {
                    "artifact_id": "commit-message-era-candidate-adjudication",
                    "version": "1.0.0",
                    "status": "sourcegraph_evidence_adjudicated_for_expansion",
                    "sourcegraph_endpoint": "https://example.sourcegraph.test",
                    "candidates": [
                        {
                            "canonical_repository_id": "example/accepted",
                            "sourcegraph_repository": "github.com/sg-evals/example",
                            "cutoff_commit": self.cutoff,
                            "candidate_event_at": "2024-02-01T00:00:00Z",
                            "candidate_commit_oid": self.event,
                            "adoption_adjudication_status": "observed",
                            "event_evidence_tier": "observed_agent_trailer",
                            "eligible_pre_adoption_message_count": pre_count,
                            "era_adjustment_eligibility": "eligible",
                        },
                        {
                            "canonical_repository_id": "example/rejected",
                            "sourcegraph_repository": "github.com/sg-evals/rejected",
                            "cutoff_commit": "a" * 40,
                            "candidate_event_at": "2024-01-01T00:00:00Z",
                            "candidate_commit_oid": "b" * 40,
                            "adoption_adjudication_status": "rejected",
                            "event_evidence_tier": "none",
                            "eligible_pre_adoption_message_count": None,
                            "era_adjustment_eligibility": "ineligible",
                        },
                        {
                            "canonical_repository_id": "example/unavailable",
                            "sourcegraph_repository": "github.com/sg-evals/unavailable",
                            "cutoff_commit": "c" * 40,
                            "candidate_event_at": "2024-01-01T00:00:00Z",
                            "candidate_commit_oid": "d" * 40,
                            "adoption_adjudication_status": "confirmed",
                            "event_evidence_tier": "confirmed_agent_commit",
                            "eligible_pre_adoption_message_count": 30,
                            "era_adjustment_eligibility": "eligible",
                        },
                    ],
                }
            )
        )
        return path

    def test_analyzes_only_adjudicated_eligible_candidates(self) -> None:
        result = build_expansion_analysis(
            adjudication_path=self._adjudication(),
            protocol_path=ROOT / "study/commit-message-authorship-heuristic.v1.json",
            cache_roots=[self.root],
        )

        self.assertEqual(result["analysis_version"], 1)
        self.assertEqual(result["sourcegraph_endpoint"], "https://example.sourcegraph.test")
        self.assertEqual(result["execution"]["candidate_count"], 3)
        self.assertEqual(result["execution"]["eligible_candidate_count"], 2)
        self.assertEqual(result["execution"]["analyzed_repository_count"], 1)
        self.assertEqual(
            result["execution"]["unavailable_repository_ids"],
            ["example/unavailable"],
        )
        repository = result["repositories"][0]
        self.assertEqual(repository["repository_id"], "example/accepted")
        self.assertEqual(repository["baseline"]["status"], "available")
        self.assertEqual(repository["baseline"]["commit_count"], 30)
        self.assertEqual(
            repository["event_evidence_tier"], "observed_agent_trailer"
        )

    def test_fails_closed_when_frozen_pre_count_does_not_match_history(self) -> None:
        with self.assertRaisesRegex(
            ExpansionAnalysisError, "pre-adoption message count"
        ):
            build_expansion_analysis(
                adjudication_path=self._adjudication(pre_count=31),
                protocol_path=ROOT
                / "study/commit-message-authorship-heuristic.v1.json",
                cache_roots=[self.root],
            )

    def test_fails_closed_when_eligible_candidate_has_too_few_messages(self) -> None:
        with self.assertRaisesRegex(
            ExpansionAnalysisError, "at least 30 pre-adoption messages"
        ):
            build_expansion_analysis(
                adjudication_path=self._adjudication(pre_count=29),
                protocol_path=ROOT
                / "study/commit-message-authorship-heuristic.v1.json",
                cache_roots=[self.root],
            )

    def test_real_adjudication_artifact_matches_schema_and_frozen_counts(self) -> None:
        artifact = json.loads(
            (
                ROOT
                / "study/commit-message-era-candidate-adjudication.v1.json"
            ).read_text()
        )
        schema = json.loads(
            (
                ROOT
                / "study/commit-message-era-candidate-adjudication.schema.json"
            ).read_text()
        )

        jsonschema.validate(artifact, schema)
        self.assertEqual(len(artifact["candidates"]), 12)
        self.assertEqual(artifact["summary"]["confirmed_count"], 2)
        self.assertEqual(artifact["summary"]["observed_count"], 4)
        self.assertEqual(artifact["summary"]["rejected_count"], 6)
        self.assertFalse(artifact["summary"]["requires_reindexing"])
        self.assertFalse(artifact["summary"]["requires_scip"])

    def test_cli_materializes_expansion_result(self) -> None:
        output = self.root / "result.json"

        exit_code = main(
            [
                "--adjudication",
                str(self._adjudication()),
                "--protocol",
                str(ROOT / "study/commit-message-authorship-heuristic.v1.json"),
                "--cache-root",
                str(self.root),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.read_text())["execution"][
                "analyzed_repository_count"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
