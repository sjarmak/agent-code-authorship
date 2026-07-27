import copy
import json
import unittest
from pathlib import Path

from authorship.candidates import load_candidates, validate_candidates


ROOT = Path(__file__).resolve().parents[1]
FRAME = ROOT / "study" / "reference-candidates.v2.json"
TARGETS = json.loads((ROOT / "study" / "targets.v1.json").read_text())
REFERENCES = json.loads((ROOT / "study" / "repositories.v1.json").read_text())


class CandidateFrameTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(FRAME.read_text())

    def errors(self, document):
        return validate_candidates(document, TARGETS, REFERENCES)

    def test_canonical_candidate_frame_is_valid_and_ordered(self):
        loaded = load_candidates(FRAME, TARGETS, REFERENCES)
        self.assertEqual(self.errors(loaded), [])
        keys = [candidate["selection_key"] for candidate in loaded["candidates"]]
        self.assertEqual(keys, sorted(keys))
        self.assertGreaterEqual(
            sum(c["proposed_label"] == "human" for c in loaded["candidates"]), 50
        )

    def test_target_or_existing_reference_overlap_is_rejected(self):
        for overlap_id in (
            TARGETS["repositories"][0]["id"],
            REFERENCES["repositories"][0]["id"],
        ):
            document = copy.deepcopy(self.document)
            document["candidates"][0]["id"] = overlap_id
            errors = self.errors(document)
            self.assertTrue(any("role overlap" in error for error in errors))

    def test_classifier_or_feature_derived_metadata_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["candidates"][0]["classifier_score"] = 0.9
        document["candidates"][1]["metadata"]["comment_rate"] = 0.2
        errors = self.errors(document)
        self.assertIn("candidate fields may not contain classifier_score", errors)
        self.assertIn("candidate metadata may not contain comment_rate", errors)

    def test_forks_and_duplicate_content_groups_are_rejected(self):
        document = copy.deepcopy(self.document)
        document["candidates"][0]["metadata"]["is_fork"] = True
        document["candidates"][1]["content_group"] = document["candidates"][0][
            "content_group"
        ]
        errors = self.errors(document)
        self.assertTrue(any("fork" in error for error in errors))
        self.assertTrue(any("content group" in error for error in errors))

    def test_selection_key_tampering_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["candidates"][0]["selection_key"] = "0" * 64
        errors = self.errors(document)
        self.assertTrue(any("selection key" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
