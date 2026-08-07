import copy
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.semantic_topology_protocol import (
    SemanticTopologyProtocolError,
    build_pilot_plan,
    canonical_sha256,
    load_pilot_protocol,
    validate_pilot_plan,
    validate_semantic_change_record,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "study" / "semantic-topology-protocol.v1.json"
PROTOCOL_SCHEMA = ROOT / "study" / "semantic-topology-protocol.schema.json"
PLAN_PATH = ROOT / "study" / "semantic-topology-pilot-plan.v1.json"
PLAN_SCHEMA = ROOT / "study" / "semantic-topology-pilot-plan.schema.json"
RECORD_SCHEMA = ROOT / "study" / "semantic-change-record.schema.json"
PRE2023_PATH = ROOT / "study" / "semantic-topology-pre2023-revisions.v1.json"
PRE2023_SCHEMA = ROOT / "study" / "semantic-topology-pre2023-revisions.schema.json"


class SemanticTopologyProtocolTests(unittest.TestCase):
    def test_canonical_digest_excludes_known_self_digest_fields_only(self):
        document = {
            "result_version": 1,
            "value": 1,
            "result_sha256": "stale",
            "input_sha256": {"source": "evidence"},
        }
        expected = canonical_sha256(
            {
                "result_version": 1,
                "value": 1,
                "input_sha256": {"source": "evidence"},
            }
        )

        self.assertEqual(canonical_sha256(document), expected)

    def setUp(self):
        self.protocol = json.loads(PROTOCOL_PATH.read_text())
        self.plan = json.loads(PLAN_PATH.read_text())

    def test_frozen_protocol_has_five_prompt_families_and_fail_closed_routes(self):
        loaded = load_pilot_protocol(PROTOCOL_PATH, ROOT)

        self.assertEqual(len(loaded["prompt_families"]), 5)
        self.assertEqual(
            [item["id"] for item in loaded["prompt_families"]],
            [
                "adoption_configuration",
                "delegated_task_intent",
                "review_validation",
                "semantic_controls",
                "change_topology",
            ],
        )
        self.assertFalse(loaded["deep_search"]["may_directly_populate_analytic_fields"])
        self.assertEqual(
            loaded["analytic_admission_routes"],
            [
                "blinded_human_adjudication",
                "deterministic_sourcegraph",
                "pinned_git",
            ],
        )
        self.assertEqual(canonical_sha256(loaded), loaded["protocol_sha256"])

    def test_protocol_and_plan_satisfy_json_schemas(self):
        jsonschema.Draft202012Validator(
            json.loads(PROTOCOL_SCHEMA.read_text())
        ).validate(self.protocol)
        jsonschema.Draft202012Validator(json.loads(PLAN_SCHEMA.read_text())).validate(
            self.plan
        )
        jsonschema.Draft202012Validator(
            json.loads(PRE2023_SCHEMA.read_text())
        ).validate(json.loads(PRE2023_PATH.read_text()))

    def test_plan_is_exactly_twenty_repositories_and_one_hundred_runs(self):
        self.assertEqual(validate_pilot_plan(self.plan, self.protocol, ROOT), [])
        self.assertEqual(len(self.plan["repositories"]), 20)
        self.assertEqual(len(self.plan["runs"]), 100)
        counts = {}
        for repository in self.plan["repositories"]:
            counts[repository["stratum"]] = counts.get(repository["stratum"], 0) + 1
        self.assertEqual(
            counts,
            {
                "agent_native": 5,
                "attributable_agent": 5,
                "policy_control": 5,
                "pre_2023_context": 5,
            },
        )
        self.assertEqual(len({run["run_id"] for run in self.plan["runs"]}), 100)
        self.assertFalse(self.plan["outcomes_consulted"])
        contextual = [
            item
            for item in self.plan["repositories"]
            if item["stratum"] == "pre_2023_context"
        ]
        self.assertTrue(
            all(item["context_revision"]["status"] == "resolved" for item in contextual)
        )
        self.assertTrue(all(item["context_revision"]["commit"] for item in contextual))
        self.assertEqual(canonical_sha256(self.plan), self.plan["plan_sha256"])

    def test_plan_rebuild_is_deterministic_and_outcome_blind(self):
        rebuilt = build_pilot_plan(self.protocol, ROOT)

        self.assertEqual(rebuilt, self.plan)
        forbidden = set(self.protocol["leakage_controls"]["forbidden_inputs"])
        self.assertTrue(forbidden.isdisjoint(rebuilt["input_artifacts"]))

    def test_fewer_than_five_native_repositories_fails_closed(self):
        broken = copy.deepcopy(self.protocol)
        broken["selection"]["agent_native_priority"] = ["gastownhall/beads"]

        with self.assertRaisesRegex(SemanticTopologyProtocolError, "fewer than five"):
            build_pilot_plan(broken, ROOT)

    def test_protocol_digest_tampering_fails_closed(self):
        broken = copy.deepcopy(self.protocol)
        broken["purpose"] += " changed after freeze"
        path = ROOT / "study" / "_invalid_semantic_protocol.json"
        try:
            path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(
                SemanticTopologyProtocolError, "protocol_sha256"
            ):
                load_pilot_protocol(path, ROOT)
        finally:
            path.unlink(missing_ok=True)

    def test_checksum_tampering_and_forbidden_inputs_fail_closed(self):
        broken = copy.deepcopy(self.plan)
        broken["input_artifacts"].append("results/survival-estimates.v2.json")

        errors = validate_pilot_plan(broken, self.protocol, ROOT)

        self.assertIn("pilot plan uses forbidden outcome input", errors)
        self.assertIn("plan_sha256 does not match canonical content", errors)

    def test_duplicate_repository_across_strata_fails_closed(self):
        broken = copy.deepcopy(self.plan)
        broken["repositories"][5]["canonical_repository_id"] = broken["repositories"][
            0
        ]["canonical_repository_id"]
        broken["plan_sha256"] = canonical_sha256(broken)

        errors = validate_pilot_plan(broken, self.protocol, ROOT)

        self.assertIn("repository appears in more than one pilot stratum", errors)

    def test_deep_search_only_claim_cannot_enter_analytic_record(self):
        record = self._valid_record()
        record["task_taxonomy"]["status"] = "observed"
        record["task_taxonomy"]["evidence_routes"] = ["deep_search"]

        errors = validate_semantic_change_record(record)

        self.assertIn(
            "observed analytic fields require an admissible verification route",
            errors,
        )

    def test_typed_missingness_is_valid_but_silent_imputation_is_not(self):
        record = self._valid_record()
        record["ownership"] = {
            "status": "unavailable",
            "reason": "no_codeowners_at_cutoff",
            "evidence_routes": [],
        }
        self.assertEqual(validate_semantic_change_record(record), [])

        record["ownership"] = {
            "status": "unavailable",
            "reason": "",
            "evidence_routes": [],
            "owner_count": 0,
        }
        errors = validate_semantic_change_record(record)
        self.assertIn("unavailable fields require a non-empty reason", errors)
        self.assertIn("unavailable fields cannot contain analytic values", errors)

    def test_deterministically_verified_observed_field_is_admissible(self):
        record = self._valid_record()
        record["task_taxonomy"] = {
            "status": "observed",
            "evidence_routes": ["deterministic_sourcegraph"],
            "label": "localized_bug_fix",
        }

        self.assertEqual(validate_semantic_change_record(record), [])

    def test_record_schema_requires_full_hunk_identity_and_provenance(self):
        schema = json.loads(RECORD_SCHEMA.read_text())
        record = self._valid_record()
        jsonschema.Draft202012Validator(schema).validate(record)

        del record["identity"]["diff_base"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(record)

    def _valid_record(self):
        unavailable = {
            "status": "unavailable",
            "reason": "not_yet_executed",
            "evidence_routes": [],
        }
        return {
            "record_version": 1,
            "identity": {
                "canonical_repository_id": "example/repo",
                "sourcegraph_name": "github.com/sg-evals/example-repo",
                "introducing_commit": "1" * 40,
                "diff_base": "2" * 40,
                "path": "src/example.py",
                "hunk_id": "sha256:" + "3" * 64,
                "cutoff_commit": "4" * 40,
            },
            "provenance": {
                "class": "attributable_agent",
                "source_artifact": "study/survival-candidates.v1.json",
                "source_sha256": "5" * 64,
                "outcomes_consulted": False,
            },
            "task_taxonomy": copy.deepcopy(unavailable),
            "blast_radius": copy.deepcopy(unavailable),
            "ownership": copy.deepcopy(unavailable),
            "tests_and_docs": copy.deepcopy(unavailable),
            "semantic_hard_negatives": copy.deepcopy(unavailable),
            "diffusion": copy.deepcopy(unavailable),
            "subsequent_changes": copy.deepcopy(unavailable),
            "human_assimilation": copy.deepcopy(unavailable),
            "observability": {
                "status": "observed",
                "evidence_routes": ["pinned_git"],
                "cutoff_reachable": True,
            },
            "uncertainty": {
                "status": "unavailable",
                "reason": "not_yet_adjudicated",
                "evidence_routes": [],
            },
            "deep_search_evidence_ids": [],
        }


if __name__ == "__main__":
    unittest.main()
