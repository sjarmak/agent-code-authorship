import unittest

from authorship.semantic_topology_report import render_report


class SemanticTopologyReportTests(unittest.TestCase):
    def test_report_separates_hypotheses_from_verified_findings(self):
        report = render_report(
            protocol={"protocol_sha256": "1" * 64},
            plan={
                "plan_sha256": "2" * 64,
                "repositories": [{}] * 20,
                "runs": [{}] * 100,
            },
            deep_inventory={
                "inventory_sha256": "3" * 64,
                "planned_run_count": 100,
                "materialized_run_count": 100,
                "counts": {"completed": 99, "error": 1},
            },
            record_inventory={
                "inventory_sha256": "4" * 64,
                "record_count": 492,
                "failure_count": 0,
            },
            candidate_inventory={
                "inventory_sha256": "5" * 64,
                "candidate_count": 120,
            },
            candidate_verification={
                "verification_sha256": "6" * 64,
                "verified_count": 75,
                "unavailable_count": 25,
                "candidate_only_count": 20,
            },
            control_candidates={
                "artifact_sha256": "7" * 64,
                "candidate_count": 15,
            },
            gates={
                "result_sha256": "8" * 64,
                "pilot_result": "validated_utility",
                "gates": {
                    "new_discovery_family": {"passes": False},
                    "task_matched_controls": {"passes": False},
                    "reliable_topology_fields": {
                        "passes": True,
                        "passing_fields": ["a", "b", "c"],
                    },
                },
            },
        )

        self.assertIn("Deep Search hypotheses are not statistical labels", report)
        self.assertIn("100 / 100", report)
        self.assertIn("Reliable topology fields | PASS", report)
        self.assertIn("Exact reproduction", report)
        self.assertIn("`" + "8" * 64 + "`", report)


if __name__ == "__main__":
    unittest.main()
