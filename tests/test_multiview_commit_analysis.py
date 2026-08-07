import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

from authorship.multiview_commit_analysis import (
    build_multiview_analysis,
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


class MultiviewCommitAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "example__repo"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        path = self.repository / "module.py"
        path.write_text("value = 1\n")
        git(self.repository, "add", "module.py")
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-q", "-m", "feat"],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Author",
                "GIT_COMMITTER_EMAIL": "author@example.com",
                "GIT_AUTHOR_DATE": "2024-06-01T09:00:00-04:00",
                "GIT_COMMITTER_DATE": "2024-06-01T13:05:00Z",
            },
        )
        self.commit = git(self.repository, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _message_analysis(self) -> Path:
        path = self.root / "message-analysis.json"
        path.write_text(
            json.dumps(
                {
                    "analysis_version": 1,
                    "study_role": "exploratory",
                    "protocol_sha256": "a" * 64,
                    "proxy_assumption": "disclosed proxy",
                    "sourcegraph_asset": {
                        "index_manifest": "study/sourcegraph-index-manifest.v3.json",
                        "canonical_repository_count": 302,
                        "indexed_repository_count": 296,
                    },
                    "execution": {
                        "selected_repository_count": 1,
                        "analyzed_repository_count": 1,
                        "unavailable_repository_ids": [],
                    },
                    "threshold_totals": {},
                    "validation": {},
                    "repositories": [
                        {
                            "repository_id": "example/repo",
                            "cutoff_commit": self.commit,
                            "start_at": "2023-01-01T00:00:00Z",
                            "commits": [
                                {
                                    "commit": self.commit,
                                    "committed_at": "2024-06-01T13:05:00Z",
                                    "added_code_lines": 1,
                                    "score": 4,
                                    "features": {
                                        "body_word_count": 80,
                                        "bullet_count": 3,
                                        "paragraph_count": 2,
                                        "sentence_count": 4,
                                        "sentence_line_rate": 0.5,
                                        "structure_count": 9,
                                        "explicit_validation": True,
                                    },
                                    "explicit_agent_provenance": True,
                                    "classifications": {"threshold_4": True},
                                }
                            ],
                        }
                    ],
                }
            )
        )
        return path

    def _references(self) -> Path:
        path = self.root / "references.json"
        path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {
                            "id": "example/repo",
                            "label": "agent",
                            "effective_date_range": [
                                "2024-01-01T00:00:00Z",
                                "2024-12-31T23:59:59Z",
                            ],
                        }
                    ]
                }
            )
        )
        return path

    def test_build_joins_message_and_timing_views_without_raw_identity(self) -> None:
        result = build_multiview_analysis(
            message_analysis_path=self._message_analysis(),
            reference_manifest_path=self._references(),
            protocol_path=ROOT / "study/multiview-commit-detector.v1.json",
            cache_roots=[self.root],
        )

        self.assertEqual(result["analysis_version"], 1)
        self.assertEqual(result["execution"]["analyzed_repository_count"], 1)
        record = result["repositories"][0]["commits"][0]
        self.assertEqual(record["reference_label"], "agent")
        self.assertEqual(record["message_score"], 4)
        self.assertEqual(record["timing_process_features"]["author_local_hour"], 9)
        self.assertEqual(record["outcome_status"], "verified_agent_provenance")
        self.assertEqual(record["outcome_category"], "verified_agent")
        self.assertNotIn("author_name", json.dumps(record))
        self.assertNotIn("author@example.com", json.dumps(record))
        self.assertEqual(
            result["evaluations"]["leave_one_repository_out"]["combined"][
                "status"
            ],
            "unavailable",
        )
        self.assertEqual(
            result["outcome_counts"],
            {
                "verified_agent": 1,
                "likely_agent_driven": 0,
                "hybrid_or_assisted": 0,
                "unknown_or_human_control": 0,
            },
        )

    def test_protocol_is_frozen_and_schema_valid(self) -> None:
        protocol = json.loads(
            (ROOT / "study/multiview-commit-detector.v1.json").read_text()
        )
        schema = json.loads(
            (ROOT / "study/multiview-commit-detector.schema.json").read_text()
        )

        jsonschema.validate(protocol, schema)
        self.assertEqual(protocol["status"], "frozen_before_multiview_scoring")
        excluded = protocol["leakage_controls"]["excluded_model_inputs"]
        self.assertIn("explicit_agent_provenance", excluded)
        self.assertIn("reference_label", excluded)
        self.assertEqual(
            set(protocol["outcome_taxonomy"]),
            {
                "verified_agent",
                "likely_agent_driven",
                "hybrid_or_assisted",
                "unknown_or_human_control",
            },
        )

    def test_cli_materializes_result(self) -> None:
        output = self.root / "result.json"
        exit_code = main(
            [
                "--message-analysis",
                str(self._message_analysis()),
                "--reference-manifest",
                str(self._references()),
                "--protocol",
                str(ROOT / "study/multiview-commit-detector.v1.json"),
                "--cache-root",
                str(self.root),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.read_text())["analysis_version"], 1)


class MultiviewCommitAnalysisE2ETests(unittest.TestCase):
    def test_help_exposes_required_inputs(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "authorship.multiview_commit_analysis", "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--message-analysis", completed.stdout)
        self.assertIn("--reference-manifest", completed.stdout)
        self.assertIn("--cache-root", completed.stdout)


if __name__ == "__main__":
    unittest.main()
