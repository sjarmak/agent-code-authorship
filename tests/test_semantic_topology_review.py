import copy
import unittest

from authorship.semantic_topology_review import (
    build_blinded_packet,
    build_distinct_author_count_packet,
    build_followup_count_packet,
    evaluate_distinct_author_count_reviews,
    evaluate_followup_count_reviews,
    evaluate_reviews,
)


class SemanticTopologyReviewTests(unittest.TestCase):
    def test_packet_is_repository_balanced_and_excludes_provenance_and_outcomes(self):
        records = []
        for repository in ("a/r", "b/r"):
            for index in range(12):
                records.append(self._record(repository, index))

        packet = build_blinded_packet(records, per_repository=10)

        self.assertEqual(packet["item_count"], 20)
        counts = {}
        for item in packet["items"]:
            repository = item["identity"]["canonical_repository_id"]
            counts[repository] = counts.get(repository, 0) + 1
            self.assertNotIn("provenance", item)
            self.assertNotIn("agent_family", str(item))
            self.assertNotIn("survival", str(item))
            self.assertEqual(
                set(item),
                {"review_item_id", "identity", "fields_to_review"},
            )
        self.assertEqual(counts, {"a/r": 10, "b/r": 10})
        self.assertFalse(packet["outcomes_consulted"])

    def test_perfect_review_agreement_passes_four_fields(self):
        packet = build_blinded_packet(
            [self._record("a/r", index) for index in range(50)],
            per_repository=50,
        )
        decisions = []
        for index, item in enumerate(packet["items"]):
            decisions.append(
                {
                    "review_item_id": item["review_item_id"],
                    "path_class": "source" if index % 3 else "test",
                    "subsequent_change_present": bool(index % 2),
                    "first_followup_author_differs": bool(index % 2),
                    "cutoff_reachable": bool(index % 2),
                }
            )
        review_a = {"reviewer_id": "a", "decisions": decisions}
        review_b = {"reviewer_id": "b", "decisions": copy.deepcopy(decisions)}

        result = evaluate_reviews(packet, review_a, review_b)

        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["field_count_passing"], 4)
        self.assertTrue(result["reliable_topology_fields_gate"])
        self.assertTrue(
            all(field["agreement"] == 1.0 for field in result["fields"].values())
        )

    def test_packet_supplements_small_repository_strata_to_target(self):
        records = [self._record("a/r", index) for index in range(7)]
        records += [self._record("b/r", index + 100) for index in range(60)]

        packet = build_blinded_packet(records, per_repository=10, target_items=50)

        self.assertEqual(packet["item_count"], 50)
        self.assertEqual(len({item["review_item_id"] for item in packet["items"]}), 50)

    def test_missing_and_disagreeing_reviews_fail_closed(self):
        packet = build_blinded_packet(
            [self._record("a/r", index) for index in range(10)],
            per_repository=10,
        )
        ids = [item["review_item_id"] for item in packet["items"]]
        review_a = {
            "reviewer_id": "a",
            "decisions": [
                {
                    "review_item_id": identifier,
                    "path_class": "source",
                    "subsequent_change_present": True,
                    "first_followup_author_differs": True,
                    "cutoff_reachable": True,
                }
                for identifier in ids
            ],
        }
        review_b = {
            "reviewer_id": "b",
            "decisions": [
                {
                    "review_item_id": identifier,
                    "path_class": "test",
                    "subsequent_change_present": False,
                    "first_followup_author_differs": False,
                    "cutoff_reachable": False,
                }
                for identifier in ids[:-1]
            ],
        }

        result = evaluate_reviews(packet, review_a, review_b)

        self.assertLess(result["coverage"], 1.0)
        self.assertFalse(result["reliable_topology_fields_gate"])
        self.assertTrue(result["missing_review_item_ids"])

    def test_followup_count_review_requires_exact_independent_agreement(self):
        packet = build_followup_count_packet(
            [self._record("a/r", index) for index in range(50)]
        )
        decisions = [
            {
                "review_item_id": item["review_item_id"],
                "followup_commit_count": index % 4,
            }
            for index, item in enumerate(packet["items"])
        ]
        result = evaluate_followup_count_reviews(
            packet,
            {"reviewer_id": "c", "decisions": decisions},
            {"reviewer_id": "d", "decisions": copy.deepcopy(decisions)},
        )

        self.assertEqual(result["observations"], 50)
        self.assertEqual(result["kappa"], 1.0)
        self.assertTrue(result["passes"])

    def test_distinct_author_count_is_a_separate_exact_metric(self):
        packet = build_distinct_author_count_packet(
            [self._record("a/r", index) for index in range(50)]
        )
        decisions = [
            {
                "review_item_id": item["review_item_id"],
                "distinct_followup_author_count": index % 3,
            }
            for index, item in enumerate(packet["items"])
        ]

        result = evaluate_distinct_author_count_reviews(
            packet,
            {"reviewer_id": "e", "decisions": decisions},
            {"reviewer_id": "f", "decisions": copy.deepcopy(decisions)},
        )

        self.assertEqual(result["observations"], 50)
        self.assertEqual(result["kappa"], 1.0)
        self.assertTrue(result["passes"])

    def _record(self, repository, index):
        return {
            "identity": {
                "canonical_repository_id": repository,
                "sourcegraph_name": "github.com/sg-evals/"
                + repository.replace("/", "-"),
                "introducing_commit": f"{index + 1:040x}",
                "diff_base": f"{index + 101:040x}",
                "path": f"src/file_{index}.py",
                "hunk_id": "sha256:" + f"{index + 1:064x}",
                "cutoff_commit": f"{index + 201:040x}",
            },
            "provenance": {"class": "attributable_agent"},
        }


if __name__ == "__main__":
    unittest.main()
