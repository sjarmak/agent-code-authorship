import copy
import unittest

from authorship.semantic_topology_results import (
    assemble_utility_gates,
    attach_reliability_uncertainty,
    build_record_summary,
    build_run_summary,
    finalize_record_missingness,
    link_candidate_evidence,
)


class SemanticTopologyResultsTests(unittest.TestCase):
    def test_candidate_links_do_not_promote_unverified_analytic_fields(self):
        record = {
            "identity": {"canonical_repository_id": "a/r"},
            "task_taxonomy": {
                "status": "unavailable",
                "reason": "pending_blinded_adjudication",
                "evidence_routes": [],
            },
            "deep_search_evidence_ids": [],
        }
        candidates = {
            "candidates": [
                {
                    "candidate_id": "sha256:" + "1" * 64,
                    "canonical_repository_id": "a/r",
                    "verification_status": "candidate",
                },
                {
                    "candidate_id": "sha256:" + "2" * 64,
                    "canonical_repository_id": "b/r",
                    "verification_status": "candidate",
                },
            ]
        }

        enriched = link_candidate_evidence([record], candidates)

        self.assertEqual(
            enriched[0]["deep_search_evidence_ids"], ["sha256:" + "1" * 64]
        )
        self.assertEqual(enriched[0]["task_taxonomy"], record["task_taxonomy"])
        self.assertEqual(record["deep_search_evidence_ids"], [])

    def test_reliable_topology_gate_combines_only_independently_passing_fields(self):
        primary = {
            "coverage": 1.0,
            "fields": {
                "path_class": {"passes": False},
                "subsequent_change_present": {"passes": True},
                "first_followup_author_differs": {"passes": True},
                "cutoff_reachable": {"passes": False},
            },
        }
        supplemental = {
            "coverage": 1.0,
            "passes": True,
            "observations": 50,
            "kappa": 1.0,
        }

        result = assemble_utility_gates(
            primary_reliability=primary,
            followup_count_reliability=supplemental,
            distinct_author_reliability={
                "coverage": 1.0,
                "passes": True,
                "observations": 50,
                "kappa": 1.0,
            },
            discovery_result=None,
            matched_control_result=None,
        )

        gate = result["gates"]["reliable_topology_fields"]
        self.assertTrue(gate["passes"])
        self.assertEqual(
            gate["passing_fields"],
            [
                "distinct_followup_author_count",
                "first_followup_author_differs",
                "subsequent_change_present",
            ],
        )
        self.assertEqual(gate["excluded_redundant_fields"], ["followup_commit_count"])
        self.assertEqual(result["pilot_result"], "validated_utility")
        self.assertFalse(result["gates"]["new_discovery_family"]["passes"])
        self.assertEqual(
            result["gates"]["new_discovery_family"]["status"], "not_identified"
        )

    def test_gate_fails_closed_without_three_fields(self):
        primary = {
            "coverage": 1.0,
            "fields": {
                "subsequent_change_present": {"passes": True},
                "path_class": {"passes": False},
            },
        }
        supplemental = copy.deepcopy(
            {"coverage": 0.7, "passes": True, "observations": 49, "kappa": 1.0}
        )

        result = assemble_utility_gates(
            primary,
            supplemental,
            distinct_author_reliability=None,
            discovery_result=None,
            matched_control_result=None,
        )

        self.assertFalse(result["gates"]["reliable_topology_fields"]["passes"])
        self.assertEqual(result["pilot_result"], "not_identified")

    def test_reliability_is_attached_as_dataset_level_uncertainty(self):
        records = [{"uncertainty": {"status": "unavailable"}}]
        primary = {
            "result_sha256": "1" * 64,
            "fields": {"path_class": {"kappa": 0.5, "passes": False}},
        }
        supplemental = {
            "result_sha256": "2" * 64,
            "kappa": 1.0,
            "passes": True,
        }

        enriched = attach_reliability_uncertainty(records, primary, supplemental)

        uncertainty = enriched[0]["uncertainty"]
        self.assertEqual(uncertainty["status"], "observed")
        self.assertEqual(uncertainty["evidence_routes"], ["pinned_git"])
        self.assertEqual(
            uncertainty["review_mode"], "independent_blinded_agent_replication"
        )
        self.assertFalse(uncertainty["field_reliability"]["path_class"]["passes"])
        self.assertTrue(
            uncertainty["field_reliability"]["followup_commit_count"]["passes"]
        )
        self.assertEqual(records[0]["uncertainty"]["status"], "unavailable")

    def test_record_summary_is_derived_and_checksummed(self):
        records = [
            {
                "identity": {"canonical_repository_id": "a/r"},
                "tests_and_docs": {"status": "observed", "path_class": "source"},
                "subsequent_changes": {"status": "observed", "commit_count": 2},
                "human_assimilation": {
                    "status": "observed",
                    "first_followup_author_differs": True,
                    "distinct_followup_author_count": 2,
                },
            },
            {
                "identity": {"canonical_repository_id": "b/r"},
                "tests_and_docs": {"status": "observed", "path_class": "test"},
                "subsequent_changes": {"status": "observed", "commit_count": 0},
                "human_assimilation": {
                    "status": "observed",
                    "first_followup_author_differs": False,
                    "distinct_followup_author_count": 0,
                },
            },
        ]

        summary = build_record_summary(records)

        self.assertEqual(summary["record_count"], 2)
        self.assertEqual(summary["repository_count"], 2)
        self.assertEqual(summary["path_class_counts"], {"source": 1, "test": 1})
        self.assertEqual(summary["subsequent_change_present_count"], 1)
        self.assertEqual(summary["first_followup_author_differs_count"], 1)
        self.assertEqual(
            summary["distinct_followup_author_count_distribution"], {"0": 1, "2": 1}
        )
        self.assertRegex(summary["summary_sha256"], r"^[0-9a-f]{64}$")

    def test_pending_missingness_becomes_final_typed_unavailability(self):
        record = {
            "task_taxonomy": {
                "status": "unavailable",
                "reason": "pending_blinded_adjudication",
                "evidence_routes": [],
            },
            "blast_radius": {
                "status": "unavailable",
                "reason": "pending_sourcegraph_code_navigation_verification",
                "evidence_routes": [],
            },
        }

        finalized = finalize_record_missingness([record])

        self.assertNotIn("pending", str(finalized))
        self.assertEqual(
            finalized[0]["task_taxonomy"]["reason"],
            "unavailable_no_blinded_task_adjudication",
        )
        self.assertEqual(
            record["task_taxonomy"]["reason"], "pending_blinded_adjudication"
        )

    def test_run_summary_reports_api_observability(self):
        runs = [
            {
                "terminal_status": "completed",
                "conversation_identity": "users/1/conversations/1",
                "raw_answer": "answer",
                "search_trace_status": "unavailable_not_exposed_by_api",
                "model_metadata": {"status": "unavailable"},
                "cited_files": [{}, {}],
                "proposed_queries": ["q"],
            },
            {
                "terminal_status": "error",
                "conversation_identity": None,
                "raw_answer": "",
                "search_trace_status": "unavailable_not_exposed_by_api",
                "model_metadata": {"status": "unavailable"},
                "cited_files": [],
                "proposed_queries": [],
            },
        ]

        summary = build_run_summary(runs)

        self.assertEqual(summary["run_count"], 2)
        self.assertEqual(
            summary["terminal_status_counts"], {"completed": 1, "error": 1}
        )
        self.assertEqual(summary["raw_answer_present_count"], 1)
        self.assertEqual(summary["cited_file_count"], 2)
        self.assertEqual(summary["proposed_query_count"], 1)
        self.assertRegex(summary["summary_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
