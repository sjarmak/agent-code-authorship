import copy
import json
import unittest
from pathlib import Path

from authorship.freeze_nonidentification import (
    NonIdentificationFreezeError,
    build_nonidentification_result,
)


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((ROOT / name).read_text())


class FreezeNonIdentificationTests(unittest.TestCase):
    def setUp(self):
        self.v1 = load("results/replication.v1.json")
        self.frame = load("study/reference-candidates.v2.json")
        self.ledger = load("study/attestations.v2.json")
        self.decision = load("study/execution-decision.v2.json")
        self.corpus_report = load("study/role-separation.v2.json")

    def build(self, **overrides):
        values = {
            "v1_result": self.v1,
            "candidate_frame": self.frame,
            "attestation_ledger": self.ledger,
            "execution_decision": self.decision,
            "v2_corpus_report": self.corpus_report,
            "artifact_hashes": {"protocol_v1": "a" * 64},
        }
        values.update(overrides)
        return build_nonidentification_result(**values)

    def test_freezes_null_estimate_and_all_locked_gate_dispositions(self):
        result = self.build()

        self.assertEqual(result["status"], "not_identified")
        self.assertIsNone(result["headline_estimate"])
        self.assertEqual(
            result["reference_eligibility"]["Python"]["human"],
            {"development": 0, "validation": 0, "eligible": False},
        )
        self.assertEqual(
            result["reference_eligibility"]["Go"]["agent"],
            {"development": 2, "validation": 1, "eligible": False},
        )
        self.assertTrue(
            all(
                gate["status"] == "not_run"
                for gate in result["locked_quantitative_gates"].values()
                if gate["status"] != "not_evaluable"
            )
        )
        self.assertEqual(
            result["locked_quantitative_gates"]["systematic_bias_trend"]["status"],
            "not_evaluable",
        )
        self.assertTrue(result["corpus"]["v2_rebuild_performed"])
        self.assertEqual(result["corpus"]["exact_collision_hashes_removed"], 19)

    def test_no_outreach_never_becomes_a_human_label(self):
        result = self.build()

        self.assertEqual(result["expansion"]["eligible_attestations"], 0)
        self.assertEqual(result["expansion"]["human_labels_inferred"], 0)
        self.assertEqual(result["expansion"]["candidate_count"], 200)

    def test_refuses_open_ledger_or_eligible_response(self):
        open_ledger = copy.deepcopy(self.ledger)
        open_ledger["status"] = "open_for_responses"
        with self.assertRaisesRegex(NonIdentificationFreezeError, "frozen"):
            self.build(attestation_ledger=open_ledger)

        labeled = copy.deepcopy(self.ledger)
        labeled["records"] = [{"disposition": "eligible"}]
        with self.assertRaisesRegex(NonIdentificationFreezeError, "eligible"):
            self.build(attestation_ledger=labeled)

    def test_refuses_decision_that_infers_labels(self):
        decision = copy.deepcopy(self.decision)
        decision["consequences"]["infer_labels_from_non_solicitation"] = True

        with self.assertRaisesRegex(NonIdentificationFreezeError, "infer"):
            self.build(execution_decision=decision)


if __name__ == "__main__":
    unittest.main()
