import copy
import json
import unittest
from pathlib import Path

from authorship.protocol_amendment import (
    AmendmentError,
    load_amendment,
    validate_amendment,
    validate_parent_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
AMENDMENT = ROOT / "study" / "protocol-amendment.v2.json"


class ProtocolAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(AMENDMENT.read_text())

    def test_canonical_amendment_is_valid_and_additive(self):
        loaded = load_amendment(AMENDMENT)
        self.assertEqual(validate_amendment(loaded), [])
        self.assertEqual(loaded["amendment_version"], 2)
        self.assertEqual(loaded["parent"]["protocol_version"], 1)
        self.assertFalse(loaded["selection"]["classifier_outputs_consulted"])
        self.assertEqual(validate_parent_artifacts(loaded, ROOT), [])

    def test_primary_human_labels_must_be_contemporary(self):
        document = copy.deepcopy(self.document)
        document["labels"]["human"]["introduced_on_or_after"] = "2022-01-01"
        errors = validate_amendment(document)
        self.assertIn(
            "primary human labels must be introduced on or after 2024-01-01",
            errors,
        )

    def test_weak_or_ambiguous_attestation_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["labels"]["human"]["evidence_tier"] = 2
        document["labels"]["human"]["required_claims"].remove(
            "no_ai_or_llm_assistance"
        )
        errors = validate_amendment(document)
        self.assertIn("human evidence must be Tier 1", errors)
        self.assertIn(
            "human attestation must explicitly claim no AI or LLM assistance",
            errors,
        )

    def test_pre_2023_code_cannot_become_a_primary_class_label(self):
        document = copy.deepcopy(self.document)
        document["historical_code"]["role"] = "primary_human_reference"
        errors = validate_amendment(document)
        self.assertIn(
            "pre-2023 code may be used only for diagnostics and sensitivity",
            errors,
        )

    def test_repository_grouping_and_role_separation_are_locked(self):
        document = copy.deepcopy(self.document)
        document["grouping"]["unit"] = "commit"
        document["roles"]["allow_target_overlap"] = True
        errors = validate_amendment(document)
        self.assertIn("labels must be grouped by repository", errors)
        self.assertIn("target/reference overlap must remain forbidden", errors)

    def test_minimum_groups_and_reserved_validation_cannot_be_weakened(self):
        document = copy.deepcopy(self.document)
        document["identification"]["minimum_substantial_groups_per_side"] = 4
        document["identification"]["reserved_validation_groups_per_side"] = 0
        errors = validate_amendment(document)
        self.assertIn("minimum substantial groups per side must be >= 5", errors)
        self.assertIn("at least one validation group per side is required", errors)

    def test_load_combines_validation_failures(self):
        broken = copy.deepcopy(self.document)
        broken["status"] = "draft"
        path = ROOT / "study" / "_invalid_amendment.json"
        try:
            path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(AmendmentError, "preregistered"):
                load_amendment(path)
        finally:
            path.unlink(missing_ok=True)

    def test_changed_parent_artifact_is_detected(self):
        document = copy.deepcopy(self.document)
        document["parent"]["sha256"]["protocol"] = "0" * 64
        errors = validate_parent_artifacts(document, ROOT)
        self.assertIn("parent protocol SHA-256 mismatch", errors)


if __name__ == "__main__":
    unittest.main()
