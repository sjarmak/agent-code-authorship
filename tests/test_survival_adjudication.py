import unittest

from authorship.survival_adjudication import blinded_record, select_cases


def event(index: int, decision: str, line_id: str | None = None):
    return {
        "case_key": f"{index:064x}",
        "line_id": line_id or f"line-{index}",
        "decision": decision,
        "repository_id": "owner/repo",
        "agent_family": "Agent",
        "provenance_tier": 2,
        "language": "Python",
        "prior_text": "before",
        "candidate_text": "after",
        "original_path": "old.py",
        "candidate_path": "new.py",
        "structural_score": 0.9,
    }


class SurvivalAdjudicationTests(unittest.TestCase):
    def test_selection_is_balanced_deterministic_and_line_deduplicated(self):
        events = [
            event(4, "unobservable"),
            event(1, "modified_candidate", "shared"),
            event(3, "unobservable", "shared"),
            event(2, "modified_candidate"),
            event(5, "unobservable"),
        ]

        selected = select_cases(events, per_class=2)

        self.assertEqual(
            [item["case_key"] for item in selected],
            [f"{index:064x}" for index in (1, 2, 4, 5)],
        )
        self.assertEqual(
            [item["selection_class"] for item in selected],
            ["accepted", "accepted", "rejected", "rejected"],
        )

    def test_blinded_record_omits_provenance_score_and_model_decision(self):
        record = blinded_record(event(1, "modified_candidate"))

        self.assertNotIn("repository_id", record)
        self.assertNotIn("agent_family", record)
        self.assertNotIn("provenance_tier", record)
        self.assertNotIn("structural_score", record)
        self.assertNotIn("decision", record)
        self.assertIsNone(record["is_same_lineage"])


if __name__ == "__main__":
    unittest.main()
