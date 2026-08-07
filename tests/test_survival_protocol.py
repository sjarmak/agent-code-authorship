import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.survival_protocol import (
    SurvivalProtocolError,
    load_survival_protocol,
    validate_survival_protocol,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "study" / "survival-protocol.v1.json"
SCHEMA = ROOT / "study" / "survival-protocol.schema.json"


class SurvivalProtocolValidationTests(unittest.TestCase):
    def test_canonical_protocol_encodes_locked_design(self):
        protocol = load_survival_protocol(PROTOCOL)

        self.assertEqual(validate_survival_protocol(protocol), [])
        self.assertEqual(protocol["snapshot"]["cutoff"], "2026-07-24T23:59:59Z")
        self.assertEqual(protocol["estimand"]["languages"], ["Python", "Go"])
        self.assertEqual(protocol["estimand"]["horizons_days"], [30, 90, 180, 365])
        self.assertEqual(
            protocol["lineage"]["states"],
            ["introduced", "unchanged", "modified", "deleted", "unobservable"],
        )
        self.assertEqual(protocol["sampling"]["maximum_repositories_per_stratum"], 25)
        self.assertEqual(protocol["validation"]["blinded_decisions"], 200)
        self.assertEqual(protocol["validation"]["minimum_structural_precision"], 0.90)
        self.assertEqual(protocol["reporting"]["failed_gate_result"], "not_identified")

    def test_protocol_digest_is_pinned_to_canonical_content(self):
        protocol = json.loads(PROTOCOL.read_text())
        expected = protocol.pop("protocol_sha256")
        canonical = json.dumps(
            protocol, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

        self.assertEqual(hashlib.sha256(canonical).hexdigest(), expected)

    def test_canonical_protocol_satisfies_json_schema(self):
        protocol = json.loads(PROTOCOL.read_text())
        schema = json.loads(SCHEMA.read_text())

        jsonschema.Draft202012Validator(schema).validate(protocol)

    def test_weaker_lineage_and_repository_gates_are_rejected(self):
        protocol = load_survival_protocol(PROTOCOL)
        protocol["identification_gates"]["minimum_lineage_coverage"] = 0.79
        protocol["identification_gates"]["minimum_repositories_per_stratum"] = 4

        errors = validate_survival_protocol(protocol)

        self.assertIn(
            "identification_gates.minimum_lineage_coverage must be >= 0.8", errors
        )
        self.assertIn(
            "identification_gates.minimum_repositories_per_stratum must be >= 5",
            errors,
        )

    def test_tiers_must_remain_separate_and_context_is_not_human(self):
        protocol = load_survival_protocol(PROTOCOL)
        protocol["provenance"]["pool_tiers_for_headline"] = True
        protocol["contextual_comparison"]["label"] = "human"

        errors = validate_survival_protocol(protocol)

        self.assertIn("provenance tiers must not be pooled for headline results", errors)
        self.assertIn(
            "contextual comparison must be labeled non_agent_attributed", errors
        )

    def test_structural_matching_cannot_enter_headline_without_validation(self):
        protocol = load_survival_protocol(PROTOCOL)
        protocol["validation"]["blinded_decisions"] = 199
        protocol["validation"]["minimum_structural_precision"] = 0.89

        errors = validate_survival_protocol(protocol)

        self.assertIn("validation.blinded_decisions must be >= 200", errors)
        self.assertIn(
            "validation.minimum_structural_precision must be >= 0.9", errors
        )

    def test_invalid_transition_is_rejected(self):
        protocol = load_survival_protocol(PROTOCOL)
        protocol["lineage"]["allowed_transitions"].append(["deleted", "unchanged"])

        errors = validate_survival_protocol(protocol)

        self.assertIn("lineage.allowed_transitions contains forbidden transitions", errors)

    def test_load_rejects_digest_mismatch(self):
        protocol = json.loads(PROTOCOL.read_text())
        protocol["purpose"] += " changed"
        broken = ROOT / "study" / "_invalid_survival_protocol.json"
        try:
            broken.write_text(json.dumps(protocol))
            with self.assertRaisesRegex(SurvivalProtocolError, "protocol_sha256"):
                load_survival_protocol(broken)
        finally:
            broken.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
