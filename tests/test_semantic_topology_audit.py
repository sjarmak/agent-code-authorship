import unittest

from authorship.semantic_topology_audit import audit_documents


class SemanticTopologyAuditTests(unittest.TestCase):
    def test_audit_proves_full_cohort_records_and_gate(self):
        strata = [
            "agent_native",
            "attributable_agent",
            "policy_control",
            "pre_2023_context",
        ]
        prompt_families = [
            "adoption_configuration",
            "delegated_task_intent",
            "review_validation",
            "semantic_controls",
            "change_topology",
        ]
        repositories = [
            {
                "canonical_repository_id": f"{stratum}/{index}",
                "stratum": stratum,
            }
            for stratum in strata
            for index in range(5)
        ]
        runs = [
            {
                "canonical_repository_id": repository["canonical_repository_id"],
                "prompt_family_id": family,
            }
            for repository in repositories
            for family in prompt_families
        ]
        result = audit_documents(
            protocol={"outcomes_consulted": False},
            plan={
                "outcomes_consulted": False,
                "repositories": repositories,
                "runs": runs,
            },
            deep_inventory={
                "inventory_sha256": "1" * 64,
                "planned_run_count": 100,
                "materialized_run_count": 100,
                "counts": {"completed": 98, "error": 2},
            },
            record_inventory={"record_count": 492, "failure_count": 0},
            enriched_inventory={
                "inventory_sha256": "2" * 64,
                "record_count": 492,
                "schema_validated_record_count": 492,
                "pending_reason_count": 0,
                "outcomes_consulted": False,
            },
            gates={
                "result_sha256": "3" * 64,
                "pilot_result": "validated_utility",
                "gates": {
                    "a": {"passes": False},
                    "b": {"passes": True},
                },
            },
            finalization={
                "deep_search_run_count": 100,
                "semantic_record_count": 492,
                "deep_search_inventory_sha256": "1" * 64,
                "enriched_inventory_sha256": "2" * 64,
                "utility_gates_sha256": "3" * 64,
            },
        )

        self.assertTrue(result["passes"])
        self.assertEqual(result["errors"], [])

    def test_audit_fails_closed_on_partial_corpus_and_no_gate(self):
        result = audit_documents(
            protocol={"outcomes_consulted": False},
            plan={"outcomes_consulted": False, "repositories": [], "runs": []},
            deep_inventory={
                "planned_run_count": 100,
                "materialized_run_count": 53,
                "counts": {"completed": 51, "error": 2},
            },
            record_inventory={"record_count": 0, "failure_count": 1},
            enriched_inventory={"record_count": 0, "outcomes_consulted": False},
            gates={"pilot_result": "not_identified", "gates": {}},
            finalization={
                "deep_search_run_count": 53,
                "semantic_record_count": 0,
            },
        )

        self.assertFalse(result["passes"])
        self.assertIn("pilot must contain exactly 20 repositories", result["errors"])
        self.assertIn("Deep Search inventory is incomplete", result["errors"])
        self.assertIn("no preregistered utility gate passed", result["errors"])


if __name__ == "__main__":
    unittest.main()
