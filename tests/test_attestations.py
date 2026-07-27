import copy
import json
import unittest
from pathlib import Path

from authorship.attestations import validate_ledger


ROOT = Path(__file__).resolve().parents[1]
FRAME = json.loads((ROOT / "study" / "reference-candidates.v2.json").read_text())
LEDGER = json.loads((ROOT / "study" / "attestations.v2.json").read_text())


def eligible_human(repo_id):
    return {
        "repository": repo_id,
        "label": "human",
        "disposition": "eligible",
        "attestor": {"identity": "maintainer", "authority": "author"},
        "scope": {
            "kind": "commit_set",
            "commits": ["a" * 40],
            "code_and_tests": True,
        },
        "claims": {"no_ai_or_llm_assistance": True},
        "basis": "personal_authorship",
        "evidence": {
            "kind": "public_github_comment",
            "url": "https://github.com/example/repo/issues/1#issuecomment-1",
        },
        "recorded_at": "2026-07-25T16:00:00Z",
    }


class AttestationLedgerTests(unittest.TestCase):
    def test_empty_open_ledger_is_valid(self):
        self.assertEqual(validate_ledger(LEDGER, FRAME), [])

    def test_exact_human_attestation_is_eligible(self):
        document = copy.deepcopy(LEDGER)
        candidate = next(
            candidate
            for candidate in FRAME["candidates"]
            if candidate["proposed_label"] == "human"
        )
        document["records"] = [eligible_human(candidate["id"])]
        self.assertEqual(validate_ledger(document, FRAME), [])

    def test_ambiguous_human_claim_or_commit_is_rejected(self):
        document = copy.deepcopy(LEDGER)
        candidate = next(
            candidate
            for candidate in FRAME["candidates"]
            if candidate["proposed_label"] == "human"
        )
        record = eligible_human(candidate["id"])
        record["claims"] = {"no_known_ai": True}
        record["scope"]["commits"] = ["short"]
        document["records"] = [record]
        errors = validate_ledger(document, FRAME)
        self.assertTrue(any("explicitly deny" in error for error in errors))
        self.assertTrue(any("full 40-character" in error for error in errors))

    def test_noncandidate_or_label_switch_is_rejected(self):
        document = copy.deepcopy(LEDGER)
        candidate = next(
            candidate
            for candidate in FRAME["candidates"]
            if candidate["proposed_label"] == "human"
        )
        record = eligible_human(candidate["id"])
        record["label"] = "agent"
        document["records"] = [record, eligible_human("unknown/repository")]
        errors = validate_ledger(document, FRAME)
        self.assertTrue(any("frozen candidate label" in error for error in errors))
        self.assertTrue(any("not in the frozen frame" in error for error in errors))

    def test_eligible_evidence_must_be_durable_and_attributed(self):
        document = copy.deepcopy(LEDGER)
        candidate = next(
            candidate
            for candidate in FRAME["candidates"]
            if candidate["proposed_label"] == "human"
        )
        record = eligible_human(candidate["id"])
        record["attestor"]["authority"] = ""
        record["evidence"] = {"kind": "private_message"}
        document["records"] = [record]
        errors = validate_ledger(document, FRAME)
        self.assertTrue(any("authority" in error for error in errors))
        self.assertTrue(any("durable" in error for error in errors))

    def test_no_solicitation_closure_is_terminal_without_inferred_labels(self):
        document = copy.deepcopy(LEDGER)
        document["status"] = "frozen_without_solicitation"
        document["closure"] = {
            "kind": "no_external_outreach",
            "applies_to_all_unsolicited_candidates": True,
            "decision_at": "2026-07-25",
            "label_inference_from_closure": False,
        }

        self.assertEqual(validate_ledger(document, FRAME), [])

    def test_no_solicitation_closure_must_cover_frame_without_label_inference(self):
        document = copy.deepcopy(LEDGER)
        document["status"] = "frozen_without_solicitation"
        document["closure"] = {
            "kind": "no_external_outreach",
            "applies_to_all_unsolicited_candidates": False,
            "decision_at": "2026-07-25",
            "label_inference_from_closure": True,
        }

        errors = validate_ledger(document, FRAME)

        self.assertTrue(any("all unsolicited candidates" in error for error in errors))
        self.assertTrue(any("must not infer labels" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
