import copy
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.protocol_v3 import (
    ProtocolV3Error,
    load_protocol_v3,
    protocol_sha256,
    validate_parent_artifacts,
    validate_protocol_v3,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "study" / "protocol.v3.json"
SCHEMA = ROOT / "study" / "protocol-v3.schema.json"


class ProtocolV3Tests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(PROTOCOL.read_text())

    def test_canonical_protocol_is_valid_and_pins_parent_artifacts(self):
        loaded = load_protocol_v3(PROTOCOL)

        self.assertEqual(validate_protocol_v3(loaded), [])
        self.assertEqual(validate_parent_artifacts(loaded, ROOT), [])
        self.assertEqual(loaded["protocol_version"], 3)
        self.assertEqual(loaded["snapshot"]["cutoff"], "2026-07-24T23:59:59Z")
        self.assertTrue(loaded["selection"]["legacy_results_known"])
        self.assertFalse(
            loaded["selection"]["outcomes_consulted_for_v3_candidate_selection"]
        )
        self.assertFalse(loaded["selection"]["new_v3_outcomes_extracted"])
        self.assertEqual(protocol_sha256(loaded), loaded["protocol_sha256"])

    def test_canonical_protocol_satisfies_json_schema(self):
        schema = json.loads(SCHEMA.read_text())

        jsonschema.Draft202012Validator(schema).validate(self.document)

    def test_population_boundaries_and_language_scope_are_locked(self):
        document = copy.deepcopy(self.document)
        document["populations"]["prevalence"]["repository_count"] = 151
        document["populations"]["survival"]["repository_count"] = 125
        document["languages"].append("TypeScript")

        errors = validate_protocol_v3(document)

        self.assertIn("prevalence population must remain 150 repositories", errors)
        self.assertIn("survival population must remain 126 repositories", errors)
        self.assertIn("primary languages must equal ['Python', 'Go']", errors)

    def test_sourcegraph_full_frame_and_non_scip_scope_are_locked(self):
        document = copy.deepcopy(self.document)
        document["sourcegraph"]["indexed_namespace"] = "github.com/other/"
        document["sourcegraph"]["full_frozen_frame_required"] = False
        document["sourcegraph"]["precise_code_intelligence_required"] = True
        document["sourcegraph"]["required_freezes"].remove("saved_query_text")

        errors = validate_protocol_v3(document)

        self.assertIn("Sourcegraph namespace must be github.com/sg-evals/", errors)
        self.assertIn("Sourcegraph must index the complete frozen frame", errors)
        self.assertIn("SCIP must not be required by protocol v3", errors)
        self.assertIn("sourcegraph.required_freezes is incomplete", errors)

    def test_human_evidence_tiers_remain_distinct_and_uncertain(self):
        document = copy.deepcopy(self.document)
        document["human_evidence"]["tiers"]["H2"]["label"] = "human_certain"
        document["human_evidence"]["tiers"]["H2"]["contamination_range"] = [0.0, 0.5]
        document["human_evidence"]["tiers"]["H3"]["clean_prehistory_days"] = 180
        document["human_evidence"]["pool_as_certain"] = True

        errors = validate_protocol_v3(document)

        self.assertIn("H2 must remain policy_human", errors)
        self.assertIn("H2 contamination range must equal [0.0, 0.2]", errors)
        self.assertIn("H3 requires at least 365 clean prehistory days", errors)
        self.assertIn("H1-H3 evidence must not be pooled as certain", errors)

    def test_new_candidate_selection_remains_outcome_blind_and_honest(self):
        document = copy.deepcopy(self.document)
        document["selection"]["legacy_results_known"] = False
        document["selection"]["outcomes_consulted_for_v3_candidate_selection"] = True
        document["selection"]["new_v3_outcomes_extracted"] = True

        errors = validate_protocol_v3(document)

        self.assertIn("selection must disclose known legacy results", errors)
        self.assertIn("v3 candidate selection must not consult outcomes", errors)
        self.assertIn(
            "new v3 outcomes must remain unextracted at preregistration", errors
        )

    def test_pre_2023_and_pre_adoption_code_are_admissible_only_as_h3(self):
        document = copy.deepcopy(self.document)
        document["human_evidence"]["tiers"]["H3"]["subtypes"] = [
            "contemporary_pre_adoption"
        ]
        document["human_evidence"]["tiers"]["H3"]["era_adjustment_required"] = False

        errors = validate_protocol_v3(document)

        self.assertIn(
            "H3 must include historical_pre_2023 and contemporary_pre_adoption",
            errors,
        )
        self.assertIn("H3 evidence requires era adjustment", errors)

    def test_adoption_event_and_estimands_cannot_label_all_post_code(self):
        document = copy.deepcopy(self.document)
        document["adoption"]["event_name"] = "actual_adoption"
        document["adoption"]["all_post_adoption_code_is_agent"] = True
        document["adoption"]["estimands"] = ["whole_repository_adoption_effect"]

        errors = validate_protocol_v3(document)

        self.assertIn("adoption event must be first_observed_adoption", errors)
        self.assertIn(
            "post-adoption code must not be labeled wholesale as agent", errors
        )
        self.assertIn("both adoption estimands are required", errors)

    def test_event_windows_and_identification_floors_cannot_be_weakened(self):
        document = copy.deepcopy(self.document)
        document["adoption"]["event_windows_days"]["primary"] = [-90, 90]
        document["identification_gates"]["minimum_adopters_per_language"] = 19
        document["identification_gates"]["minimum_controls_per_language"] = 9
        document["identification_gates"]["minimum_agent_family_repositories"] = 7

        errors = validate_protocol_v3(document)

        self.assertIn("primary event window must equal [-180, 180]", errors)
        self.assertIn("minimum adopters per language must be >= 20", errors)
        self.assertIn("minimum controls per language must be >= 10", errors)
        self.assertIn("minimum agent-family repositories must be >= 8", errors)

    def test_era_adjustment_and_primary_classifier_are_locked(self):
        document = copy.deepcopy(self.document)
        document["era_adjustment"]["method"] = "calendar_year_covariate"
        document["authorship"]["primary_model"] = "gradient_boosted_trees"
        document["authorship"]["partial_identification"]["enabled"] = False

        errors = validate_protocol_v3(document)

        self.assertIn(
            "era adjustment must use within_repository_difference_in_differences",
            errors,
        )
        self.assertIn("primary classifier must remain l2_logistic_regression", errors)
        self.assertIn("partial identification must remain enabled", errors)

    def test_survival_estimators_weighting_and_censoring_are_locked(self):
        document = copy.deepcopy(self.document)
        document["survival"]["primary_estimator"] = "complete_case_mean"
        document["survival"]["secondary_estimator"] = "state_proportions"
        document["survival"]["lineage_loss"] = "drop"
        document["survival"]["primary_weighting"] = "line"
        document["survival"]["required_invariants"].remove(
            "survival_is_monotone_nonincreasing"
        )

        errors = validate_protocol_v3(document)

        self.assertIn(
            "primary survival estimator must be repository_balanced_kaplan_meier",
            errors,
        )
        self.assertIn(
            "secondary survival estimator must be aalen_johansen_state_occupancy",
            errors,
        )
        self.assertIn("lineage loss must censor at last observation", errors)
        self.assertIn("primary survival weighting must be repository", errors)
        self.assertIn("survival.required_invariants is incomplete", errors)

    def test_digest_and_parent_hash_changes_fail_closed(self):
        document = copy.deepcopy(self.document)
        document["purpose"] += " changed"
        document["parent"]["sha256"]["protocol_v1"] = "0" * 64

        errors = validate_protocol_v3(document)
        parent_errors = validate_parent_artifacts(document, ROOT)

        self.assertIn("protocol_sha256 does not match canonical content", errors)
        self.assertIn("parent protocol_v1 SHA-256 mismatch", parent_errors)

    def test_loader_combines_validation_errors(self):
        broken = copy.deepcopy(self.document)
        broken["status"] = "draft"
        path = ROOT / "study" / "_invalid_protocol_v3.json"
        try:
            path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(ProtocolV3Error, "preregistered"):
                load_protocol_v3(path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
