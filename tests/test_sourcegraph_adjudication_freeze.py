import copy
import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.sourcegraph_adjudication_freeze import (
    AdjudicationFreezeError,
    adjudication_bundle_sha256,
    adjudication_freeze_sha256,
    build_adjudication_freeze,
    validate_adjudication_freeze,
)
from authorship.sourcegraph_discovery import (
    agreement_audit_selected,
    evidence_packet_sha256,
)
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def evidence_packet(packet_type="adoption_event", commit_oid=SHA_B):
    family_id = (
        "agent_trailer_commits"
        if packet_type == "adoption_event"
        else "ai_ban_policy_introduction_diffs"
    )
    packet = {
        "packet_version": 3,
        "packet_id": "1" * 64 if packet_type == "adoption_event" else "2" * 64,
        "packet_type": packet_type,
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": SHA_A,
        "indexed_revision_oid": SHA_A,
        "query_family_id": family_id,
        "rendered_query": (
            f"repo:^github\\.com/sg-evals/org-repo$@{SHA_A} "
            "type:commit patternType:regexp"
        ),
        "rendered_query_sha256": "",
        "sourcegraph_result_ids": [f"result-{packet_type}"],
        "candidate_event": {
            "commit_oid": commit_oid,
            "observed_at": "2025-07-01T00:00:00Z",
        },
        "raw_evidence": [
            {
                "kind": (
                    "commit_message"
                    if packet_type == "adoption_event"
                    else "diff_added"
                ),
                "commit_oid": commit_oid,
                "path": None,
                "line": None,
                "value": "Frozen evidence",
                "source_url": f"https://github.com/org/repo/commit/{commit_oid}",
            }
        ],
        "outcomes_consulted": False,
    }
    packet["rendered_query_sha256"] = hashlib.sha256(
        packet["rendered_query"].encode()
    ).hexdigest()
    packet["packet_sha256"] = evidence_packet_sha256(packet)
    return packet


def packet_index(*packets, pending=()):
    document = {
        "packet_index_version": 3,
        "specification_sha256": "",
        "source_result_manifest_sha256s": ["4" * 64],
        "packet_count": len(packets),
        "pending_file_enrichment_count": len(pending),
        "sourcegraph_line_number_basis": "zero_based_as_returned_by_graphql",
        "source_url_line_anchor_basis": "one_based",
        "packets": list(packets),
        "pending_file_enrichments": list(pending),
        "outcomes_consulted": False,
    }
    return document


def review(reviewer_id, decision, stage="primary"):
    return {
        "stage": stage,
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "reviewer_version": "reviewer-protocol-v3",
        "decision": decision,
        "outcome_blind": True,
        "peer_review_blind": True,
        "rationale": "Applied the frozen rubric to the packet.",
    }


def bundle(packet, reviews):
    document = {
        "bundle_version": 3,
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"],
        "packet_type": packet["packet_type"],
        "reviews": reviews,
    }
    document["bundle_sha256"] = adjudication_bundle_sha256(document)
    return document


def audit_selected_packet(specification):
    packet = evidence_packet()
    audit_specification = specification["adjudication"]["agreement_audit"]
    for candidate in range(256):
        packet["packet_id"] = f"{candidate:064x}"
        packet["packet_sha256"] = evidence_packet_sha256(packet)
        if agreement_audit_selected(packet["packet_sha256"], audit_specification):
            return packet
    raise AssertionError("could not construct an agreement-audit packet")


class SourcegraphAdjudicationFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.specification = json.loads(
            (root / "study" / "sourcegraph-discovery.v3.json").read_text()
        )
        cls.bundle_schema = json.loads(
            (root / "study" / "sourcegraph-adjudication-bundle.schema.json").read_text()
        )
        cls.freeze_schema = json.loads(
            (root / "study" / "sourcegraph-adjudication-freeze.schema.json").read_text()
        )

    def prepare_index(self, *packets, pending=()):
        document = packet_index(*packets, pending=pending)
        document["specification_sha256"] = self.specification["specification_sha256"]
        document["packet_index_sha256"] = packet_index_sha256(document)
        return document

    def test_agreement_sets_final_decision_and_audit_does_not_relabel(self):
        packet = audit_selected_packet(self.specification)
        index = self.prepare_index(packet)
        reviews = [
            review("reviewer-a", "confirmed"),
            review("reviewer-b", "confirmed"),
            review("reviewer-c", "rejected", "agreement_audit"),
        ]

        frozen = build_adjudication_freeze(
            self.specification, index, [bundle(packet, reviews)]
        )

        record = frozen["catalogs"]["adoption_events"]["records"][0]
        self.assertEqual(record["final_decision"], "confirmed")
        self.assertEqual(record["final_basis"], "primary_agreement")
        self.assertTrue(record["primary_analysis_included"])
        self.assertEqual(record["agreement_audit_decision"], "rejected")
        self.assertFalse(record["agreement_audit_matches_final"])
        self.assertEqual(frozen["agreement_audit"]["decision_mismatch_count"], 1)

    def test_disagreement_uses_resolution_review(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        reviews = [
            review("reviewer-a", "confirmed"),
            review("reviewer-b", "observed"),
            review("reviewer-c", "observed", "resolution"),
        ]

        frozen = build_adjudication_freeze(
            self.specification, index, [bundle(packet, reviews)]
        )

        record = frozen["catalogs"]["adoption_events"]["records"][0]
        self.assertEqual(record["final_decision"], "observed")
        self.assertEqual(record["final_basis"], "resolution")
        self.assertFalse(record["primary_analysis_included"])
        self.assertTrue(record["sensitivity_analysis_included"])

    def test_ai_ban_catalog_accepts_only_admissible_final_decisions(self):
        admissible = evidence_packet("ai_ban_policy", SHA_B)
        rejected = copy.deepcopy(evidence_packet("ai_ban_policy", SHA_C))
        rejected["packet_id"] = "5" * 64
        rejected["packet_sha256"] = evidence_packet_sha256(rejected)
        index = self.prepare_index(admissible, rejected)
        bundles = [
            bundle(
                admissible,
                [
                    review("reviewer-a", "admissible"),
                    review("reviewer-b", "admissible"),
                ],
            ),
            bundle(
                rejected,
                [
                    review("reviewer-c", "rejected"),
                    review("reviewer-d", "rejected"),
                ],
            ),
        ]

        frozen = build_adjudication_freeze(self.specification, index, bundles)

        catalog = frozen["catalogs"]["ai_ban_policies"]
        self.assertEqual(catalog["record_count"], 2)
        self.assertEqual(catalog["primary_analysis_included_count"], 1)
        self.assertEqual(
            [record["final_decision"] for record in catalog["records"]],
            ["admissible", "rejected"],
        )

    def test_bundle_coverage_must_match_packet_index_exactly(self):
        first = evidence_packet()
        second = copy.deepcopy(evidence_packet(commit_oid=SHA_C))
        second["packet_id"] = "6" * 64
        second["packet_sha256"] = evidence_packet_sha256(second)
        index = self.prepare_index(first, second)
        first_bundle = bundle(
            first,
            [review("reviewer-a", "confirmed"), review("reviewer-b", "confirmed")],
        )

        with self.assertRaisesRegex(
            AdjudicationFreezeError, "one adjudication bundle per packet"
        ):
            build_adjudication_freeze(self.specification, index, [first_bundle])
        with self.assertRaisesRegex(
            AdjudicationFreezeError, "one adjudication bundle per packet"
        ):
            build_adjudication_freeze(
                self.specification, index, [first_bundle, first_bundle]
            )

    def test_bundle_binding_and_checksum_are_enforced(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        valid = bundle(
            packet,
            [review("reviewer-a", "confirmed"), review("reviewer-b", "confirmed")],
        )
        wrong_packet = {**valid, "packet_sha256": "9" * 64}
        wrong_checksum = {**valid, "bundle_sha256": "8" * 64}

        for candidate in (wrong_packet, wrong_checksum):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AdjudicationFreezeError):
                    build_adjudication_freeze(self.specification, index, [candidate])

    def test_incomplete_file_enrichment_blocks_freeze(self):
        index = self.prepare_index(
            pending=[
                {
                    "canonical_repository_id": "org/repo",
                    "sourcegraph_name": "github.com/sg-evals/org-repo",
                    "query_family_id": "adoption_announcement_files",
                    "result_manifest_sha256": "4" * 64,
                    "sourcegraph_result_id": "result-file",
                    "path": "README.md",
                    "line_numbers": [0],
                    "required_enrichment": "blame_introducing_commit",
                }
            ]
        )

        with self.assertRaisesRegex(
            AdjudicationFreezeError, "pending file enrichments"
        ):
            build_adjudication_freeze(self.specification, index, [])

    def test_invalid_reviews_fail_closed(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        invalid = bundle(
            packet,
            [
                review("same-reviewer", "confirmed"),
                review("same-reviewer", "confirmed"),
            ],
        )

        with self.assertRaisesRegex(
            AdjudicationFreezeError, "primary reviewers must be independent"
        ):
            build_adjudication_freeze(self.specification, index, [invalid])

    def test_review_bundle_rejects_unfrozen_semantic_fields(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        first = review("reviewer-a", "confirmed")
        first["peer_decision"] = "confirmed"
        invalid = bundle(
            packet,
            [first, review("reviewer-b", "confirmed")],
        )

        with self.assertRaisesRegex(AdjudicationFreezeError, "unapproved fields"):
            build_adjudication_freeze(self.specification, index, [invalid])

    def test_freeze_rejects_mutated_frozen_specification(self):
        changed = copy.deepcopy(self.specification)
        changed["adjudication"]["agreement_audit"]["sample_basis_points"] = 10_000
        index = self.prepare_index()

        with self.assertRaisesRegex(
            AdjudicationFreezeError, "specification_sha256 does not match"
        ):
            build_adjudication_freeze(changed, index, [])

    def test_output_is_deterministic_and_validates_against_schemas(self):
        adoption = evidence_packet()
        policy = evidence_packet("ai_ban_policy", SHA_C)
        index = self.prepare_index(adoption, policy)
        bundles = [
            bundle(
                adoption,
                [
                    review("reviewer-a", "confirmed"),
                    review("reviewer-b", "confirmed"),
                ],
            ),
            bundle(
                policy,
                [
                    review("reviewer-c", "admissible"),
                    review("reviewer-d", "admissible"),
                ],
            ),
        ]

        forward = build_adjudication_freeze(self.specification, index, bundles)
        reverse = build_adjudication_freeze(
            self.specification, index, list(reversed(bundles))
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(validate_adjudication_freeze(forward, self.specification), [])
        self.assertEqual(
            forward["adjudication_freeze_sha256"],
            adjudication_freeze_sha256(forward),
        )
        jsonschema.Draft202012Validator(self.bundle_schema).validate(bundles[0])
        jsonschema.Draft202012Validator(self.freeze_schema).validate(forward)

    def test_validator_detects_catalog_mutation_without_crashing(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        valid_bundle = bundle(
            packet,
            [review("reviewer-a", "confirmed"), review("reviewer-b", "confirmed")],
        )
        frozen = build_adjudication_freeze(self.specification, index, [valid_bundle])
        changed = copy.deepcopy(frozen)
        changed["catalogs"]["adoption_events"]["records"][0][
            "final_decision"
        ] = "rejected"
        changed["catalogs"]["adoption_events"]["primary_analysis_included_count"] = 99

        errors = validate_adjudication_freeze(changed, self.specification)

        self.assertIn("adjudication_freeze_sha256 does not match", errors)
        self.assertIn("adoption_events catalog checksum does not match", errors)
        self.assertIn(
            "adoption_events primary_analysis_included_count does not match",
            errors,
        )
        self.assertTrue(
            validate_adjudication_freeze(
                {"catalogs": {"adoption_events": None}}, self.specification
            )
        )
        malformed = copy.deepcopy(frozen)
        malformed["catalogs"]["adoption_events"]["records"][0][
            "packet_type"
        ] = "not-a-packet-type"
        self.assertTrue(validate_adjudication_freeze(malformed, self.specification))

    def test_validator_rejects_internally_inconsistent_audit_fields(self):
        packet = evidence_packet()
        index = self.prepare_index(packet)
        valid_bundle = bundle(
            packet,
            [review("reviewer-a", "confirmed"), review("reviewer-b", "confirmed")],
        )
        frozen = build_adjudication_freeze(self.specification, index, [valid_bundle])
        changed = copy.deepcopy(frozen)
        record = changed["catalogs"]["adoption_events"]["records"][0]
        record["agreement_audit_decision"] = "rejected"
        record["agreement_audit_matches_final"] = True

        self.assertIn(
            "adoption_events agreement audit fields are inconsistent",
            validate_adjudication_freeze(changed, self.specification),
        )

    def test_empty_completed_packet_index_freezes_empty_catalogs(self):
        index = self.prepare_index()

        frozen = build_adjudication_freeze(self.specification, index, [])

        self.assertEqual(frozen["packet_count"], 0)
        self.assertEqual(frozen["catalogs"]["adoption_events"]["records"], [])
        self.assertEqual(frozen["catalogs"]["ai_ban_policies"]["records"], [])


if __name__ == "__main__":
    unittest.main()
