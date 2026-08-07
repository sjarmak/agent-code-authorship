import copy
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.sourcegraph_discovery import (
    agreement_audit_selected,
    evidence_packet_sha256,
    specification_sha256,
    validate_adjudication_bundle,
    validate_discovery_specification,
    validate_evidence_packet,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
PACKET_ID = "1" * 64


def packet():
    document = {
        "packet_version": 3,
        "packet_id": PACKET_ID,
        "packet_type": "adoption_event",
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": SHA_A,
        "indexed_revision_oid": SHA_A,
        "query_family_id": "agent_trailer_commits",
        "rendered_query": (
            f"repo:^github\\.com/sg-evals/org-repo$@{SHA_A} type:commit "
            "patternType:regexp fork:yes archived:yes count:all"
        ),
        "rendered_query_sha256": "",
        "sourcegraph_result_ids": ["result-1"],
        "candidate_event": {
            "commit_oid": SHA_B,
            "observed_at": "2025-07-01T00:00:00Z",
        },
        "raw_evidence": [
            {
                "kind": "commit_message",
                "commit_oid": SHA_B,
                "path": None,
                "line": None,
                "value": "Generated-by: example-agent",
                "source_url": "https://example.invalid/result-1",
            }
        ],
        "outcomes_consulted": False,
    }
    import hashlib

    document["rendered_query_sha256"] = hashlib.sha256(
        document["rendered_query"].encode()
    ).hexdigest()
    document["packet_sha256"] = evidence_packet_sha256(document)
    return document


def primary_review(reviewer_id, decision):
    return {
        "stage": "primary",
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "reviewer_version": "reviewer-protocol-v3",
        "decision": decision,
        "outcome_blind": True,
        "peer_review_blind": True,
        "rationale": "Evidence was adjudicated under the frozen rubric.",
    }


class SourcegraphDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.specification = json.loads(
            (root / "study" / "sourcegraph-discovery.v3.json").read_text()
        )
        cls.specification_schema = json.loads(
            (root / "study" / "sourcegraph-discovery.schema.json").read_text()
        )
        cls.packet_schema = json.loads(
            (root / "study" / "sourcegraph-evidence-packet.schema.json").read_text()
        )

    def test_frozen_specification_is_complete_and_checksummed(self):
        self.assertEqual(validate_discovery_specification(self.specification), [])
        self.assertEqual(
            self.specification["specification_sha256"],
            specification_sha256(self.specification),
        )
        jsonschema.Draft202012Validator(self.specification_schema).validate(
            self.specification
        )
        channels = {
            channel
            for family in self.specification["query_families"]
            for channel in family["evidence_channels"]
        }
        self.assertEqual(
            channels,
            {
                "adoption_announcement",
                "agent_identity",
                "commit",
                "diff",
                "generated_code_disclosure",
                "policy",
                "revision_history",
                "trailer",
            },
        )
        for family in self.specification["query_families"]:
            self.assertIn("patternType:", family["query_template"])
            self.assertNotIn("SCIP", family["required_capabilities"])
            self.assertNotIn(
                "precise_code_intelligence", family["required_capabilities"]
            )

    def test_specification_digest_detects_query_mutation(self):
        changed = copy.deepcopy(self.specification)
        changed["query_families"][0]["query_template"] += " altered"

        self.assertNotEqual(
            changed["specification_sha256"], specification_sha256(changed)
        )
        self.assertIn(
            "specification_sha256 does not match",
            validate_discovery_specification(changed),
        )

    def test_specification_validator_rejects_weakened_guards(self):
        changed = copy.deepcopy(self.specification)
        changed["query_families"][1]["id"] = changed["query_families"][0]["id"]
        changed["query_families"][0]["saved_search_eligible"] = False
        changed["query_families"][0]["required_capabilities"].append("SCIP")
        changed["query_families"][0]["evidence_channels"] = []
        changed["adjudication"]["independent_primary_reviews"] = 1
        changed["adjudication"]["outcome_blind"] = False
        changed["adjudication"]["peer_review_blind"] = False
        changed["adjudication"]["agreement_audit"]["sample_basis_points"] = 0
        changed["adjudication"]["agreement_audit"]["deterministic"] = False
        changed["adjudication"]["disagreement_resolution"]["reviewers"] = 2

        errors = validate_discovery_specification(changed)

        self.assertIn("query family identifiers must be unique", errors)
        self.assertIn(
            "query families do not cover the required evidence channels", errors
        )
        self.assertTrue(any("unapproved capability" in error for error in errors))
        self.assertTrue(any("saved-search eligible" in error for error in errors))
        self.assertIn("exactly two independent primary reviews must be frozen", errors)
        self.assertIn("adjudication must be outcome blind", errors)
        self.assertIn("primary reviewers must be blind to each other", errors)

    def test_specification_validator_rejects_malformed_repository_exclusions(self):
        changed = copy.deepcopy(self.specification)
        changed["execution"]["repository_exclusions"] = {
            "source_artifact": "",
            "source_artifact_sha256": "not-a-sha",
            "repositories": [
                {
                    "canonical_repository_id": "org/repo",
                    "disposition": "unknown",
                    "reason": "",
                },
                {
                    "canonical_repository_id": "org/repo",
                    "disposition": "hold_private",
                    "reason": "Duplicate.",
                },
            ],
        }
        changed["specification_sha256"] = specification_sha256(changed)

        errors = validate_discovery_specification(changed)

        self.assertTrue(any("source artifact path" in error for error in errors))
        self.assertTrue(any("source artifact SHA-256" in error for error in errors))
        self.assertTrue(any("disposition" in error for error in errors))
        self.assertTrue(any("reason" in error for error in errors))
        self.assertIn("repository exclusion identifiers must be unique", errors)

    def test_evidence_packet_is_structural_and_content_addressed(self):
        document = packet()

        self.assertEqual(validate_evidence_packet(document, self.specification), [])
        jsonschema.Draft202012Validator(self.packet_schema).validate(document)
        changed = copy.deepcopy(document)
        changed["raw_evidence"][0]["value"] = "different"
        self.assertIn(
            "packet_sha256 does not match",
            validate_evidence_packet(changed, self.specification),
        )
        wrong_scope = packet()
        wrong_scope["rendered_query"] = wrong_scope["rendered_query"].replace(
            "sg-evals/org-repo", "sg-evals/other-repo"
        )
        import hashlib

        wrong_scope["rendered_query_sha256"] = hashlib.sha256(
            wrong_scope["rendered_query"].encode()
        ).hexdigest()
        wrong_scope["packet_sha256"] = evidence_packet_sha256(wrong_scope)
        self.assertIn(
            "rendered_query does not pin sourcegraph_name",
            validate_evidence_packet(wrong_scope, self.specification),
        )

    def test_two_independent_blinded_primary_reviews_are_required(self):
        document = {
            "packet_sha256": packet()["packet_sha256"],
            "packet_type": "adoption_event",
            "reviews": [primary_review("reviewer-a", "confirmed")],
        }

        errors = validate_adjudication_bundle(document, self.specification)

        self.assertIn("exactly two primary reviews are required", errors)
        document["reviews"].append(primary_review("reviewer-a", "confirmed"))
        self.assertIn(
            "primary reviewers must be independent",
            validate_adjudication_bundle(document, self.specification),
        )

    def test_packet_validator_rejects_unfrozen_or_incomplete_evidence(self):
        document = packet()
        document["packet_version"] = 2
        document["packet_type"] = "unknown"
        document["outcomes_consulted"] = True
        document["indexed_revision_oid"] = SHA_B
        document["query_family_id"] = "not_frozen"
        document["sourcegraph_result_ids"] = []
        document["raw_evidence"] = []
        document["candidate_event"]["observed_at"] = "2027-01-01T00:00:00Z"
        document["packet_sha256"] = evidence_packet_sha256(document)

        errors = validate_evidence_packet(document, self.specification)

        self.assertIn("packet_version must equal 3", errors)
        self.assertIn("packet_type is invalid", errors)
        self.assertIn("evidence packet must be outcome blind", errors)
        self.assertIn("indexed_revision_oid must equal cutoff_commit", errors)
        self.assertIn("sourcegraph_result_ids must be non-empty", errors)
        self.assertIn("raw_evidence must be non-empty", errors)
        self.assertIn("candidate event is after the frozen cutoff", errors)

    def test_disagreement_requires_blinded_third_reviewer(self):
        document = {
            "packet_sha256": packet()["packet_sha256"],
            "packet_type": "adoption_event",
            "reviews": [
                primary_review("reviewer-a", "confirmed"),
                primary_review("reviewer-b", "observed"),
            ],
        }

        self.assertIn(
            "disagreement requires one resolution review",
            validate_adjudication_bundle(document, self.specification),
        )
        document["reviews"].append(
            {
                **primary_review("reviewer-c", "confirmed"),
                "stage": "resolution",
            }
        )
        self.assertEqual(validate_adjudication_bundle(document, self.specification), [])
        document["reviews"].append(
            {
                **primary_review("reviewer-d", "observed"),
                "stage": "agreement_audit",
            }
        )
        self.assertIn(
            "agreement audit review is invalid for a disagreement",
            validate_adjudication_bundle(document, self.specification),
        )

    def test_deterministic_agreement_sample_requires_audit_review(self):
        specification = copy.deepcopy(self.specification)
        specification["adjudication"]["agreement_audit"]["sample_basis_points"] = 10_000
        document = {
            "packet_sha256": packet()["packet_sha256"],
            "packet_type": "adoption_event",
            "reviews": [
                primary_review("reviewer-a", "confirmed"),
                primary_review("reviewer-b", "confirmed"),
            ],
        }

        self.assertTrue(
            agreement_audit_selected(
                document["packet_sha256"],
                specification["adjudication"]["agreement_audit"],
            )
        )
        self.assertIn(
            "selected agreement requires one audit review",
            validate_adjudication_bundle(document, specification),
        )
        document["reviews"].append(
            {
                **primary_review("reviewer-c", "confirmed"),
                "stage": "agreement_audit",
            }
        )
        self.assertEqual(validate_adjudication_bundle(document, specification), [])

    def test_model_review_records_model_and_prompt_versions(self):
        model_review = {
            **primary_review("reviewer-a", "confirmed"),
            "reviewer_kind": "model",
        }
        document = {
            "packet_sha256": packet()["packet_sha256"],
            "packet_type": "adoption_event",
            "reviews": [
                model_review,
                primary_review("reviewer-b", "confirmed"),
            ],
        }

        self.assertIn(
            "model reviews must freeze provider, model, and prompt versions",
            validate_adjudication_bundle(document, self.specification),
        )
        model_review.update(
            {
                "provider": "provider",
                "model_id": "model",
                "model_version": "2026-07-01",
                "prompt_sha256": "f" * 64,
            }
        )
        self.assertNotIn(
            "model reviews must freeze provider, model, and prompt versions",
            validate_adjudication_bundle(document, self.specification),
        )

    def test_review_validator_rejects_invalid_stage_decision_and_blinding(self):
        invalid = primary_review("reviewer-a", "not-a-decision")
        invalid.update(
            {
                "stage": "unknown",
                "outcome_blind": False,
                "peer_review_blind": False,
                "reviewer_kind": "unknown",
                "reviewer_version": "",
                "rationale": "",
            }
        )
        document = {
            "packet_sha256": packet()["packet_sha256"],
            "packet_type": "adoption_event",
            "reviews": [invalid, primary_review("reviewer-b", "confirmed")],
        }

        errors = validate_adjudication_bundle(document, self.specification)

        self.assertIn("every review must be outcome blind", errors)
        self.assertIn("every review must be peer-review blind", errors)
        self.assertIn("every review must record reviewer_kind", errors)
        self.assertIn("every review must record reviewer_version", errors)
        self.assertIn("every review must record a rationale", errors)
        self.assertIn("exactly two primary reviews are required", errors)


if __name__ == "__main__":
    unittest.main()
