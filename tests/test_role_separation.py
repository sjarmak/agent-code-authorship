import unittest

from authorship.role_separation import (
    RoleSeparationError,
    enforce_role_separation,
)


def record(repo, digest, role):
    return {"repo": repo, "content_sha256": digest, "role": role}


class RoleSeparationTests(unittest.TestCase):
    def test_repository_cannot_span_partitions(self):
        partitions = {
            "modeling": [record("owner/repo", "a", "reference")],
            "validation": [record("owner/repo", "b", "reference_validation")],
            "target": [],
        }
        with self.assertRaisesRegex(RoleSeparationError, "repository"):
            enforce_role_separation(partitions)

    def test_content_group_cannot_span_partitions(self):
        partitions = {
            "modeling": [record("owner/a", "a", "reference")],
            "validation": [record("owner/b", "b", "reference_validation")],
            "target": [],
        }
        with self.assertRaisesRegex(RoleSeparationError, "content group"):
            enforce_role_separation(
                partitions,
                content_groups={"owner/a": "family", "owner/b": "family"},
            )

    def test_target_collision_is_removed_only_from_labeled_data(self):
        partitions = {
            "modeling": [
                record("human/a", "shared", "reference"),
                record("human/a", "model-only", "reference"),
            ],
            "validation": [],
            "target": [record("target/a", "shared", "target")],
        }
        filtered, report = enforce_role_separation(partitions)
        self.assertEqual(
            [item["content_sha256"] for item in filtered["modeling"]],
            ["model-only"],
        )
        self.assertEqual(len(filtered["target"]), 1)
        self.assertEqual(report["dropped_records"]["modeling"], 1)
        self.assertEqual(report["collision_hashes"], 1)

    def test_model_validation_collision_is_removed_from_both(self):
        partitions = {
            "modeling": [record("human/a", "shared", "reference")],
            "validation": [record("human/b", "shared", "reference_validation")],
            "target": [],
        }
        filtered, report = enforce_role_separation(partitions)
        self.assertEqual(filtered["modeling"], [])
        self.assertEqual(filtered["validation"], [])
        self.assertEqual(report["dropped_records"]["modeling"], 1)
        self.assertEqual(report["dropped_records"]["validation"], 1)

    def test_cross_group_duplicate_within_labeled_partition_is_removed(self):
        partitions = {
            "modeling": [
                record("human/a", "shared", "reference"),
                record("human/b", "shared", "reference"),
            ],
            "validation": [],
            "target": [],
        }
        filtered, report = enforce_role_separation(partitions)
        self.assertEqual(filtered["modeling"], [])
        self.assertEqual(report["collision_hashes"], 1)

    def test_duplicate_within_one_repository_is_retained(self):
        partitions = {
            "modeling": [
                record("human/a", "same", "reference"),
                record("human/a", "same", "reference"),
            ],
            "validation": [],
            "target": [],
        }
        filtered, report = enforce_role_separation(partitions)
        self.assertEqual(len(filtered["modeling"]), 2)
        self.assertEqual(report["collision_hashes"], 0)

    def test_report_is_deterministic_and_records_input_output_counts(self):
        partitions = {
            "validation": [],
            "target": [record("target/a", "target", "target")],
            "modeling": [record("human/a", "model", "reference")],
        }
        filtered, report = enforce_role_separation(partitions)
        self.assertEqual(list(filtered), ["modeling", "target", "validation"])
        self.assertEqual(
            report["input_records"],
            {"modeling": 1, "target": 1, "validation": 0},
        )
        self.assertEqual(report["output_records"], report["input_records"])


if __name__ == "__main__":
    unittest.main()
