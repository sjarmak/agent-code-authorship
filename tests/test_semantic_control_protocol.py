import copy
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.semantic_control_protocol import (
    ControlProtocolError,
    canonical_sha256,
    freeze_protocol,
    validate_protocol,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "study" / "semantic-control-protocol.v1.json"
SCHEMA = ROOT / "study" / "semantic-control-protocol.schema.json"


class SemanticControlProtocolTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads(PROTOCOL.read_text())

    def test_frozen_protocol_is_schema_valid_and_self_checking(self):
        jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(
            self.protocol
        )
        self.assertEqual(validate_protocol(self.protocol, ROOT), [])
        self.assertEqual(
            canonical_sha256(self.protocol), self.protocol["protocol_sha256"]
        )

    def test_protocol_forbids_outcomes_during_matching(self):
        self.assertNotIn(
            "results/semantic-change-records.enriched.inventory.v1.json",
            self.protocol["input_artifacts"],
        )
        self.assertFalse(
            self.protocol["treated_identity_construction"]["followup_artifacts_read"]
        )
        forbidden = set(self.protocol["outcome_blindness"]["forbidden_match_inputs"])
        self.assertEqual(
            forbidden,
            {
                "subsequent_change_present",
                "time_to_first_followup_days",
                "first_followup_author_differs",
                "distinct_followup_author_count",
            },
        )
        self.assertEqual(
            self.protocol["matching"]["exact"],
            ["repository", "path_class", "task_class"],
        )
        self.assertEqual(
            self.protocol["matching"]["method"],
            "exact_plus_robust_scaled_euclidean_nearest_neighbor",
        )
        self.assertFalse(self.protocol["matching"]["replacement"])

    def test_common_cutoff_and_censoring_are_locked(self):
        observation = self.protocol["observation"]
        self.assertEqual(observation["cutoff_rule"], "treated_repository_frozen_cutoff")
        self.assertEqual(observation["minimum_followup_days"], 0)
        self.assertEqual(
            observation["time_estimand"], "restricted_mean_time_to_first_followup"
        )
        self.assertEqual(observation["horizon_days"], 180)

    def test_balance_and_identification_gates_are_explicit(self):
        gates = self.protocol["identification_gates"]
        self.assertEqual(gates["maximum_absolute_smd"], 0.1)
        self.assertEqual(gates["minimum_matched_fraction"], 0.7)
        self.assertEqual(gates["minimum_effective_sample_size"], 100)
        self.assertEqual(gates["minimum_repositories"], 3)

    def test_outcome_input_or_digest_tampering_fails_closed(self):
        broken = copy.deepcopy(self.protocol)
        broken["input_artifacts"].append(
            "results/semantic-topology-record-summary.v1.json"
        )
        broken["protocol_sha256"] = canonical_sha256(broken)
        self.assertIn(
            "protocol includes forbidden outcome artifact",
            validate_protocol(broken, ROOT),
        )

        broken = copy.deepcopy(self.protocol)
        broken["matching"]["seed"] += 1
        self.assertIn("protocol_sha256 mismatch", validate_protocol(broken, ROOT))

    def test_freeze_is_deterministic(self):
        self.assertEqual(freeze_protocol(ROOT), self.protocol)

    def test_missing_input_fails_closed(self):
        broken = copy.deepcopy(self.protocol)
        original = broken["input_artifacts"][0]
        broken["input_artifacts"][0] = "study/does-not-exist.json"
        digest = broken["input_sha256"].pop(original)
        broken["input_sha256"]["study/does-not-exist.json"] = digest
        broken["protocol_sha256"] = canonical_sha256(broken)
        with self.assertRaises(ControlProtocolError):
            validate_protocol(broken, ROOT, raise_on_missing=True)
