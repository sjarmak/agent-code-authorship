import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

from authorship.commit_message_analysis import (
    CommitMessageAnalysisError,
    analyze_git_repository,
    build_analysis,
    main,
)
from authorship.commit_message_heuristic import (
    baseline_from_messages,
    classify_message,
    extract_style_features,
    normalize_message,
)


ROOT = Path(__file__).resolve().parents[1]


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CommitMessageHeuristicUnitTests(unittest.TestCase):
    def test_protocol_is_frozen_and_schema_valid(self):
        protocol = json.loads(
            (ROOT / "study/commit-message-authorship-heuristic.v1.json").read_text()
        )
        schema = json.loads(
            (
                ROOT / "study/commit-message-authorship-heuristic.schema.json"
            ).read_text()
        )

        jsonschema.validate(protocol, schema)
        self.assertEqual(protocol["status"], "frozen_before_scoring")
        self.assertIn("not independently verified", protocol["proxy_assumption"])

    def test_normalization_removes_direct_provenance_but_preserves_structure(self):
        message = """feat: explain the change

This updates the parser because malformed input must fail clearly.

- Adds validation.
- Runs the tests.
- Documents the boundary.

Co-authored-by: Claude Opus <noreply@anthropic.com>
Generated-by: Cursor https://cursor.com/run/123
Implementation notes from Claude are at https://example.com/run/456.
"""

        normalized = normalize_message(message)

        self.assertNotIn("Claude", normalized)
        self.assertNotIn("anthropic.com", normalized)
        self.assertNotIn("Cursor", normalized)
        self.assertNotIn("https://", normalized)
        self.assertNotIn("[tool]", normalized)
        self.assertNotIn("[url]", normalized)
        self.assertEqual(normalized.count("- "), 3)
        self.assertIn("because malformed input", normalized)

    def test_surface_features_capture_long_structured_validation_prose(self):
        body = "\n".join(
            [
                "This change separates parsing from validation so failures are explicit.",
                "",
                "- Adds a strict input boundary.",
                "- Preserves the existing output contract.",
                "- Covers malformed records.",
                "",
                "Tests: ran pytest and all checks passed. "
                + " ".join(["detail"] * 60)
                + ".",
                "The implementation remains deterministic.",
            ]
        )

        features = extract_style_features(f"feat: validate records\n\n{body}")

        self.assertGreaterEqual(features["body_word_count"], 80)
        self.assertEqual(features["bullet_count"], 3)
        self.assertGreaterEqual(features["paragraph_count"], 3)
        self.assertTrue(features["explicit_validation"])

    def test_repository_baseline_requires_thirty_contemporary_messages(self):
        unavailable = baseline_from_messages(
            ["fix: short"] * 29,
            repository_id="example/repo",
        )
        available = baseline_from_messages(
            [f"fix: change {index}\n\nword " * (index % 5 + 1) for index in range(30)],
            repository_id="example/repo",
        )

        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(available["status"], "available")
        self.assertEqual(available["commit_count"], 30)
        self.assertIsInstance(available["body_word_count_p90"], float)

    def test_primary_and_sensitivity_thresholds_are_deterministic(self):
        message = """feat: make record validation explicit

This change validates every record before it reaches persistence.

- Rejects missing identifiers.
- Rejects invalid timestamps.
- Preserves the public response shape.

Tests: ran the unit and integration checks; all tests passed.
The build also completed successfully. Additional implementation details are
documented here so reviewers can trace each boundary without reading the code.
"""
        baseline = {
            "status": "available",
            "repository_id": "example/repo",
            "commit_count": 30,
            "body_word_count_p90": 35.0,
            "structure_count_p90": 5.0,
        }

        first = classify_message(message, baseline=baseline)
        second = classify_message(message, baseline=baseline)

        self.assertEqual(first, second)
        self.assertTrue(first["classifications"]["threshold_3"])
        self.assertTrue(first["classifications"]["threshold_4"])
        self.assertIn("local_length_outlier", first["signals"])
        self.assertNotIn("provenance_marker", first["signals"])


class CommitMessageAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "example__repo"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "Test Author")
        git(self.repository, "config", "user.email", "test@example.com")

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, name: str, contents: str, message: str, date: str) -> str:
        path = self.repository / name
        path.write_text(contents)
        git(self.repository, "add", name)
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-q", "-m", message],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            },
        )
        return git(self.repository, "rev-parse", "HEAD")

    def test_analysis_attributes_added_code_from_detected_commit(self):
        self.commit(
            "module.py",
            "one = 1\n",
            "fix: initialize value",
            "2024-01-01T00:00:00Z",
        )
        agent_message = """feat: validate every input record

This change validates input before persistence and makes failures explicit.

- Rejects missing identifiers.
- Rejects malformed timestamps.
- Preserves the response contract.

Tests: ran pytest and all checks passed. The implementation is deliberately
documented in detail so each boundary can be reviewed independently. More
context follows to explain the behavior, compatibility, and validation path.
"""
        cutoff = self.commit(
            "module.py",
            "one = 1\ntwo = 2\nthree = 3\n",
            agent_message,
            "2024-02-01T00:00:00Z",
        )

        result = analyze_git_repository(
            repository_id="example/repo",
            path=self.repository,
            cutoff_commit=cutoff,
            start_at="2023-01-01T00:00:00Z",
            adoption_at="2024-01-15T00:00:00Z",
            thresholds=(3, 4, 5),
        )

        self.assertEqual(result["eligible_commit_count"], 2)
        self.assertEqual(result["eligible_added_line_count"], 3)
        detected = result["thresholds"]["3"]
        self.assertEqual(detected["detected_commit_count"], 1)
        self.assertEqual(detected["agent_attributed_added_line_count"], 2)
        self.assertEqual(detected["agent_attributed_added_line_share"], 2 / 3)
        contrast = result["within_repository"]["thresholds"]["3"]
        self.assertEqual(contrast["pre"]["detected_commit_share"], 0.0)
        self.assertEqual(contrast["post"]["detected_commit_share"], 1.0)
        self.assertEqual(contrast["commit_share_change"], 1.0)
        self.assertNotIn("message", result["commits"][0])
        self.assertIn("normalized_message_sha256", result["commits"][0])

        control_result = analyze_git_repository(
            repository_id="example/control",
            path=self.repository,
            cutoff_commit=cutoff,
            start_at="2023-01-01T00:00:00Z",
            adoption_at=None,
            infer_adoption_from_provenance=False,
            thresholds=(3, 4, 5),
        )
        self.assertEqual(control_result["adoption_source"], "unavailable")
        self.assertEqual(control_result["within_repository"]["status"], "unavailable")

    def test_unknown_cutoff_fails_closed(self):
        with self.assertRaisesRegex(
            CommitMessageAnalysisError, "cutoff commit is not available"
        ):
            analyze_git_repository(
                repository_id="example/repo",
                path=self.repository,
                cutoff_commit="0" * 40,
                start_at="2023-01-01T00:00:00Z",
                adoption_at=None,
                thresholds=(3, 4, 5),
            )

    def test_reachable_pull_request_commit_is_included_but_merge_is_excluded(self):
        initial = self.commit(
            "module.py",
            "one = 1\n",
            "fix: initialize value",
            "2024-01-01T00:00:00Z",
        )
        default_branch = git(self.repository, "branch", "--show-current")
        git(self.repository, "checkout", "-q", "-b", "feature")
        feature = self.commit(
            "feature.py",
            "feature = True\n",
            """feat: validate the feature

- Adds a feature boundary.
- Runs the relevant tests.
- Records the expected result.

Tests: ran pytest and all checks passed. The implementation notes explain the
validation path and compatibility behavior for reviewers.
""",
            "2024-02-01T00:00:00Z",
        )
        git(self.repository, "checkout", "-q", default_branch)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "merge",
                "--no-ff",
                "-q",
                "-m",
                "merge feature",
                feature,
            ],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": "2024-03-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-03-01T00:00:00Z",
            },
        )
        cutoff = git(self.repository, "rev-parse", "HEAD")

        result = analyze_git_repository(
            repository_id="example/repo",
            path=self.repository,
            cutoff_commit=cutoff,
            start_at="2023-01-01T00:00:00Z",
            adoption_at=None,
            thresholds=(3, 4, 5),
        )

        self.assertEqual(
            {commit["commit"] for commit in result["commits"]},
            {initial, feature},
        )
        self.assertEqual(result["thresholds"]["3"]["detected_commit_count"], 1)

    def test_pure_rename_does_not_create_agent_attributed_lines(self):
        self.commit(
            "module.py",
            "one = 1\ntwo = 2\n",
            "feat: add module",
            "2024-01-01T00:00:00Z",
        )
        git(self.repository, "mv", "module.py", "renamed.py")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "commit",
                "-q",
                "-m",
                "refactor: rename module",
            ],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": "2024-02-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-02-01T00:00:00Z",
            },
        )
        cutoff = git(self.repository, "rev-parse", "HEAD")

        result = analyze_git_repository(
            repository_id="example/repo",
            path=self.repository,
            cutoff_commit=cutoff,
            start_at="2023-01-01T00:00:00Z",
            adoption_at=None,
        )

        self.assertEqual(result["eligible_added_line_count"], 2)
        self.assertEqual(result["commits"][-1]["added_code_lines"], 0)

    def test_manifest_workflow_materializes_validation_result(self):
        cutoff = self.commit(
            "module.py",
            "one = 1\n",
            """feat: document validation

- Adds one check.
- Runs one test.
- Records one result.

Tests: ran pytest and all checks passed. This message includes enough
implementation context to describe the validation boundary clearly.
""",
            "2024-02-01T00:00:00Z",
        )
        index_manifest = self.root / "index.json"
        reference_manifest = self.root / "references.json"
        output = self.root / "result.json"
        index_manifest.write_text(
            json.dumps(
                {
                    "repository_count": 1,
                    "repositories": [
                        {
                            "canonical_repository_id": "example/repo",
                            "cutoff_commit": cutoff,
                            "roles": ["classifier_agent_reference"],
                        }
                    ],
                }
            )
        )
        reference_manifest.write_text(
            json.dumps(
                {
                    "repositories": [
                        {
                            "id": "example/repo",
                            "label": "agent",
                            "effective_date_range": [
                                None,
                                "2024-12-31T23:59:59Z",
                            ],
                        }
                    ]
                }
            )
        )

        result = build_analysis(
            index_manifest_path=index_manifest,
            reference_manifest_path=reference_manifest,
            protocol_path=(
                ROOT / "study/commit-message-authorship-heuristic.v1.json"
            ),
            cache_roots=[self.root],
            roles=["classifier_agent_reference"],
        )

        self.assertEqual(result["execution"]["analyzed_repository_count"], 1)
        self.assertEqual(result["sourcegraph_asset"]["indexed_repository_count"], 1)
        self.assertEqual(result["validation"]["3"]["tp"], 1)
        self.assertEqual(
            main(
                [
                    "--index-manifest",
                    str(index_manifest),
                    "--reference-manifest",
                    str(reference_manifest),
                    "--protocol",
                    str(
                        ROOT
                        / "study/commit-message-authorship-heuristic.v1.json"
                    ),
                    "--cache-root",
                    str(self.root),
                    "--output",
                    str(output),
                ]
            ),
            0,
        )
        self.assertEqual(
            json.loads(output.read_text())["execution"][
                "analyzed_repository_count"
            ],
            1,
        )
        bad_protocol = self.root / "bad-protocol.json"
        protocol = json.loads(
            (
                ROOT / "study/commit-message-authorship-heuristic.v1.json"
            ).read_text()
        )
        protocol["decision_rule"]["primary_threshold"] = 5
        bad_protocol.write_text(json.dumps(protocol))
        with self.assertRaisesRegex(
            CommitMessageAnalysisError, "frozen v1 rule"
        ):
            build_analysis(
                index_manifest_path=index_manifest,
                reference_manifest_path=reference_manifest,
                protocol_path=bad_protocol,
                cache_roots=[self.root],
                roles=["classifier_agent_reference"],
            )


class CommitMessageAnalysisE2ETests(unittest.TestCase):
    def test_cli_materializes_machine_readable_result(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "authorship.commit_message_analysis",
                "--help",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--index-manifest", completed.stdout)
        self.assertIn("--output", completed.stdout)


if __name__ == "__main__":
    unittest.main()
