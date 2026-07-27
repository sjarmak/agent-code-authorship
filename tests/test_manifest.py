import copy
import unittest
from pathlib import Path

from authorship.manifest import (
    eligibility_by_language,
    load_manifest,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "study" / "repositories.v1.json"


class ManifestValidationTests(unittest.TestCase):
    def test_canonical_manifest_is_structurally_valid(self):
        manifest = load_manifest(MANIFEST)

        self.assertEqual(validate_manifest(manifest), [])

    def test_confirmed_projects_are_pinned_at_the_cutoff(self):
        manifest = load_manifest(MANIFEST)
        by_id = {entry["id"]: entry for entry in manifest["repositories"]}

        self.assertEqual(
            by_id["gastownhall/gascity"]["snapshot"]["commit"],
            "bd7c9dac0b305b94893f3382f66cb129536d1be4",
        )
        self.assertEqual(
            by_id["gastownhall/beads"]["snapshot"]["commit"],
            "41f3bfe6f22d9b8b90775cae01a785d99df3f63c",
        )
        self.assertEqual(
            by_id["gastownhall/gascity"]["evidence"]["scope"],
            "entire_repository_history",
        )

    def test_human_reference_requires_temporal_repo_local_evidence(self):
        manifest = load_manifest(MANIFEST)
        broken = copy.deepcopy(manifest)
        human = next(r for r in broken["repositories"] if r["label"] == "human")
        human["evidence"]["repository_local"] = False
        del human["evidence"]["effective_from"]

        errors = validate_manifest(broken)

        self.assertTrue(any("repository-local" in error for error in errors))
        self.assertTrue(any("effective_from" in error for error in errors))

    def test_repository_cannot_hold_multiple_roles_in_one_specification(self):
        manifest = load_manifest(MANIFEST)
        duplicate = copy.deepcopy(manifest["repositories"][0])
        duplicate["role"] = "dedicated_validation"
        manifest["repositories"].append(duplicate)

        errors = validate_manifest(manifest)

        self.assertTrue(any("multiple roles" in error for error in errors))

    def test_insufficient_group_diversity_fails_identification_gate(self):
        manifest = load_manifest(MANIFEST)
        eligibility = eligibility_by_language(manifest)

        self.assertFalse(eligibility["Go"]["identified"])
        self.assertIn("fewer than 5", " ".join(eligibility["Go"]["reasons"]))

    def test_validation_group_does_not_count_toward_development_minimum(self):
        repositories = [
            {
                "id": f"human/{index}",
                "label": "human",
                "role": "reference",
                "languages": ["Python"],
            }
            for index in range(4)
        ]
        repositories += [
            {
                "id": f"agent/{index}",
                "label": "agent",
                "role": "reference",
                "languages": ["Python"],
            }
            for index in range(5)
        ]
        repositories += [
            {
                "id": "human/validation",
                "label": "human",
                "role": "dedicated_validation",
                "languages": ["Python"],
            },
            {
                "id": "agent/validation",
                "label": "agent",
                "role": "dedicated_validation",
                "languages": ["Python"],
            },
        ]

        result = eligibility_by_language({"repositories": repositories})["Python"]

        self.assertFalse(result["identified"])
        self.assertEqual(result["development_groups"]["human"], 4)
        self.assertEqual(result["dedicated_validation_groups"]["human"], 1)
        self.assertIn("human has 4 development groups", " ".join(result["reasons"]))

    def test_both_labels_require_held_out_validation(self):
        repositories = [
            {
                "id": f"{label}/{index}",
                "label": label,
                "role": "reference",
                "languages": ["Python"],
            }
            for label in ("human", "agent")
            for index in range(5)
        ]
        repositories.append(
            {
                "id": "agent/validation",
                "label": "agent",
                "role": "dedicated_validation",
                "languages": ["Python"],
            }
        )

        result = eligibility_by_language({"repositories": repositories})["Python"]

        self.assertFalse(result["identified"])
        self.assertIn(
            "human has no dedicated validation group", " ".join(result["reasons"])
        )


if __name__ == "__main__":
    unittest.main()
