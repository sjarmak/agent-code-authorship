import subprocess
import tempfile
import unittest
from pathlib import Path

from authorship.commit_timing_features import extract_repository_evidence
from authorship.multiview_evaluation import (
    evaluate_era_holdout,
    evaluate_leave_one_repository_out,
    feature_names,
    feature_vector,
)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CommitTimingFeatureIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-q")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit(
        self,
        *,
        path: str,
        content: str,
        message: str,
        author_name: str,
        author_email: str,
        authored_at: str,
        committed_at: str,
    ) -> str:
        target = self.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(self.repository, "add", "-A")
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-q", "-m", message],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": "Landing Bot",
                "GIT_COMMITTER_EMAIL": "landing@example.com",
                "GIT_AUTHOR_DATE": authored_at,
                "GIT_COMMITTER_DATE": committed_at,
            },
        )
        return git(self.repository, "rev-parse", "HEAD")

    def test_extracts_hashed_identity_timing_bursts_and_diff_shape(self) -> None:
        first = self.commit(
            path="module.py",
            content="one = 1\n",
            message="first",
            author_name="Example Author",
            author_email="author@example.com",
            authored_at="2024-01-01T09:00:00+02:00",
            committed_at="2024-01-01T07:05:00Z",
        )
        second = self.commit(
            path="module.py",
            content="one = 1\ntwo = 2\n",
            message="second",
            author_name="Example Author",
            author_email="author@example.com",
            authored_at="2024-01-01T09:10:00+02:00",
            committed_at="2024-01-01T07:15:00Z",
        )

        evidence = extract_repository_evidence(
            repository_id="example/repo",
            path=self.repository,
            cutoff_commit=second,
            start_at="2023-01-01T00:00:00Z",
            eligible_commits={first, second},
        )

        self.assertEqual(tuple(evidence), (first, second))
        first_row = evidence[first]
        second_row = evidence[second]
        self.assertNotIn("author_name", first_row)
        self.assertNotIn("author_email", first_row)
        self.assertEqual(len(first_row["author_identity_sha256"]), 64)
        self.assertNotEqual(
            first_row["author_identity_sha256"],
            first_row["committer_identity_sha256"],
        )
        self.assertEqual(first_row["author_local_hour"], 9)
        self.assertEqual(first_row["author_utc_offset_minutes"], 120)
        self.assertEqual(first_row["author_committer_delta_seconds"], 300.0)
        self.assertIsNone(first_row["repository_previous_gap_seconds"])
        self.assertEqual(second_row["repository_previous_gap_seconds"], 600.0)
        self.assertEqual(second_row["same_author_previous_gap_seconds"], 600.0)
        self.assertEqual(second_row["same_author_prior_15m_count"], 1)
        self.assertEqual(second_row["files_changed"], 1)
        self.assertEqual(second_row["lines_added"], 1)
        self.assertEqual(second_row["lines_deleted"], 0)

    def test_pure_rename_is_recorded_without_inventing_added_lines(self) -> None:
        first = self.commit(
            path="old.py",
            content="value = 1\n",
            message="first",
            author_name="Example Author",
            author_email="author@example.com",
            authored_at="2024-01-01T00:00:00Z",
            committed_at="2024-01-01T00:00:00Z",
        )
        git(self.repository, "mv", "old.py", "new.py")
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-q", "-m", "rename"],
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Author",
                "GIT_COMMITTER_EMAIL": "author@example.com",
                "GIT_AUTHOR_DATE": "2024-01-02T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-01-02T00:00:00Z",
            },
        )
        renamed = git(self.repository, "rev-parse", "HEAD")

        evidence = extract_repository_evidence(
            repository_id="example/repo",
            path=self.repository,
            cutoff_commit=renamed,
            start_at="2023-01-01T00:00:00Z",
            eligible_commits={first, renamed},
        )

        self.assertEqual(evidence[renamed]["rename_entry_count"], 1)
        self.assertEqual(evidence[renamed]["lines_added"], 0)
        self.assertEqual(evidence[renamed]["lines_deleted"], 0)


def labeled_record(
    repository_id: str,
    label: str,
    committed_at: str,
    *,
    gap: float,
    burst: int,
    message_score: int,
) -> dict:
    return {
        "repository_id": repository_id,
        "reference_label": label,
        "committed_at": committed_at,
        "message_score": message_score,
        "message_features": {
            "body_word_count": message_score * 20,
            "bullet_count": message_score,
            "paragraph_count": message_score,
            "sentence_count": message_score,
            "sentence_line_rate": message_score / 7,
            "structure_count": message_score * 2,
            "explicit_validation": message_score >= 4,
        },
        "timing_process_features": {
            "author_committer_delta_seconds": gap / 2,
            "repository_previous_gap_seconds": gap,
            "same_author_previous_gap_seconds": gap,
            "same_author_prior_15m_count": burst,
            "author_local_hour": 12,
            "author_weekday": 2,
            "author_utc_offset_minutes": 0,
            "parent_count": 1,
            "files_changed": burst + 1,
            "lines_added": burst * 10,
            "lines_deleted": burst,
            "binary_file_count": 0,
            "rename_entry_count": 0,
        },
        "explicit_agent_provenance": label == "agent",
        "author_identity_sha256": f"secret-{repository_id}",
    }


class MultiviewEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = []
        for repository_id, label, gap, burst, score in (
            ("agent/one", "agent", 60.0, 4, 5),
            ("agent/two", "agent", 90.0, 3, 4),
            ("human/one", "human", 3600.0, 0, 1),
            ("human/two", "human", 7200.0, 0, 0),
        ):
            self.records.extend(
                labeled_record(
                    repository_id,
                    label,
                    f"{year}-06-01T12:00:00Z",
                    gap=gap,
                    burst=burst,
                    message_score=score,
                )
                for year in (2024, 2025)
            )

    def test_feature_views_exclude_labels_provenance_and_identity(self) -> None:
        names = feature_names("combined")
        vector = feature_vector(self.records[0], "combined")

        self.assertEqual(len(names), len(vector))
        self.assertNotIn("reference_label", names)
        self.assertNotIn("explicit_agent_provenance", names)
        self.assertNotIn("author_identity_sha256", names)
        self.assertIn("log_repository_previous_gap_seconds", names)
        self.assertIn("message_score", names)

    def test_leave_one_repository_out_evaluation_is_deterministic(self) -> None:
        first = evaluate_leave_one_repository_out(self.records, view="combined")
        second = evaluate_leave_one_repository_out(self.records, view="combined")

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "available")
        self.assertEqual(first["held_out_repository_count"], 4)
        self.assertEqual(first["labeled_commit_count"], 8)
        self.assertGreater(first["average_precision"], 0.9)
        self.assertNotIn("predictions", first)
        human = {
            item["repository_id"]: item
            for item in first["repository_metrics"]
            if item["reference_label"] == "human"
        }
        self.assertEqual(set(human), {"human/one", "human/two"})
        self.assertEqual(human["human/one"]["false_positive_count"], 0)
        self.assertEqual(human["human/one"]["false_positive_rate"], 0.0)

    def test_era_holdout_is_explicit_about_train_and_test_windows(self) -> None:
        result = evaluate_era_holdout(
            self.records,
            view="timing_process",
            cutoff="2025-01-01T00:00:00Z",
        )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["training_commit_count"], 4)
        self.assertEqual(result["test_commit_count"], 4)
        self.assertEqual(result["cutoff"], "2025-01-01T00:00:00Z")

    def test_repository_metrics_reject_mixed_reference_labels(self) -> None:
        records = [dict(record) for record in self.records]
        records[0]["reference_label"] = "human"

        with self.assertRaisesRegex(ValueError, "single reference label"):
            evaluate_leave_one_repository_out(records, view="timing_process")


if __name__ == "__main__":
    unittest.main()
