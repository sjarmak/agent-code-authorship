import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from authorship.sourcegraph_index_inventory import (
    _resolve_remote_git_snapshot,
    collect_github_audits,
    collect_sourcegraph_audits,
    generate_index_manifest,
    main as inventory_main,
)
from authorship.sourcegraph_index_manifest import (
    IndexManifestError,
    build_index_manifest,
    validate_index_manifest,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_A = "1" * 40
TREE_B = "2" * 40
CUTOFF = "2026-07-24T23:59:59Z"


def repository_audits(*repository_ids):
    return {
        repository_id: {
            "license": {
                "status": "declared",
                "spdx_id": "MIT",
                "url": f"https://github.com/{repository_id}/blob/HEAD/LICENSE",
            },
            "access": {
                "status": "public",
                "archived": False,
                "default_branch": "main",
            },
        }
        for repository_id in repository_ids
    }


def index_audits(*repository_ids):
    return {
        repository_id: {
            "direct": {
                "name": f"github.com/{repository_id}",
                "state": "not_indexed",
                "head_oid": None,
                "cutoff_state": "not_accessible",
                "cutoff_oid": None,
            },
            "mirror": {
                "name": f"github.com/sg-evals/{repository_id.replace('/', '-')}",
                "state": "indexed",
                "head_oid": SHA_A,
                "cutoff_state": "accessible",
                "cutoff_oid": SHA_A,
            },
        }
        for repository_id in repository_ids
    }


class SourcegraphIndexManifestTests(unittest.TestCase):
    def test_audit_only_cli_preserves_frozen_manifest_outputs(self):
        manifest = {"repositories": []}
        repository_audit = {"kind": "repository"}
        sourcegraph_audit = {"kind": "sourcegraph"}
        with tempfile.TemporaryDirectory() as temporary, patch(
            "authorship.sourcegraph_index_inventory.generate_index_manifest",
            return_value=(manifest, repository_audit, sourcegraph_audit),
        ), patch(
            "authorship.sourcegraph_index_inventory.validate_index_manifest",
            return_value=[],
        ), patch(
            "authorship.sourcegraph_index_inventory.write_manifest"
        ) as write, patch(
            "sys.argv",
            [
                "sourcegraph_index_inventory",
                "--survival-inventory",
                "inventory.json",
                "--root",
                temporary,
                "--audit-only",
            ],
        ):
            inventory_main()

        write.assert_called_once_with(
            Path(temporary) / "study" / "sourcegraph-index-audit.v3.json",
            sourcegraph_audit,
        )

    def test_generates_manifest_and_both_read_only_audits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "study").mkdir()
            (root / "data").mkdir()
            documents = {
                "study/targets.v1.json": {
                    "cutoff": CUTOFF,
                    "repositories": [
                        {
                            "id": "org/target",
                            "url": "https://github.com/org/target",
                            "snapshot": {"commit": SHA_A, "tree": TREE_A},
                        }
                    ],
                },
                "study/survival-candidates.v1.json": {
                    "candidates": [
                        {
                            "repository_id": "org/survive",
                            "repository_url": "https://github.com/org/survive",
                        }
                    ]
                },
                "study/repositories.v1.json": {
                    "repositories": [
                        {
                            "id": "org/reference",
                            "url": "https://github.com/org/reference",
                            "role": "reference",
                            "label": "agent",
                            "snapshot": {"commit": SHA_A, "tree": TREE_B},
                        }
                    ]
                },
                "data/control_evidence.json": {
                    "repos": {"org/control": {"policy": []}}
                },
            }
            for relative, document in documents.items():
                (root / relative).write_text(json.dumps(document))
            inventory_path = root / "git-inventory.json"
            inventory_path.write_text(
                json.dumps(
                    {
                        "cutoff": CUTOFF,
                        "repositories": [
                            {
                                "repository_id": "org/survive",
                                "cutoff_commit": SHA_B,
                                "cutoff_tree": "3" * 40,
                            }
                        ],
                    }
                )
            )
            repository_ids = [
                "org/control",
                "org/reference",
                "org/survive",
                "org/target",
            ]
            with (
                patch(
                    "authorship.sourcegraph_index_inventory._control_source_urls",
                    return_value={"org/control": "https://github.com/org/control"},
                ),
                patch(
                    "authorship.sourcegraph_index_inventory._collect_missing_snapshots",
                    return_value={"org/control": {"commit": SHA_A, "tree": "4" * 40}},
                ),
                patch(
                    "authorship.sourcegraph_index_inventory._repository_records",
                    return_value=repository_audits(*repository_ids),
                ),
                patch(
                    "authorship.sourcegraph_index_inventory.collect_sourcegraph_audits",
                    return_value=index_audits(*repository_ids),
                ),
                patch(
                    "authorship.sourcegraph_index_inventory.fork_map",
                    return_value={},
                ),
                patch(
                    "authorship.sourcegraph_index_inventory.check_auth",
                    return_value="researcher",
                ),
            ):
                manifest, repository_audit, sourcegraph_audit = generate_index_manifest(
                    root=root,
                    survival_inventory_path=inventory_path,
                    observed_at="2026-07-27T12:00:00Z",
                )

        self.assertEqual(manifest["repository_count"], 4)
        self.assertEqual(repository_audit["repository_count"], 4)
        self.assertEqual(sourcegraph_audit["sourcegraph_user"], "researcher")

    def test_resolves_non_github_cutoff_snapshot_from_remote_git(self):
        completed = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="origin/main\n", stderr=""),
            SimpleNamespace(returncode=0, stdout=f"{SHA_A}\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=f"2026-07-24T20:00:00+00:00\n{TREE_A}\n",
                stderr="",
            ),
        ]

        with patch(
            "authorship.sourcegraph_index_inventory.subprocess.run",
            side_effect=completed,
        ) as run:
            snapshot = _resolve_remote_git_snapshot("https://codeberg.org/org/repo")

        self.assertEqual(
            snapshot,
            {
                "commit": SHA_A,
                "committed_at": "2026-07-24T20:00:00+00:00",
                "tree": TREE_A,
            },
        )
        rev_list = run.call_args_list[2].args[0]
        self.assertIn("origin/main", rev_list)
        self.assertNotIn("--all", rev_list)

    def test_collects_direct_and_mirror_sourcegraph_state_in_batches(self):
        calls = []

        def query_api(query):
            calls.append(query)
            return {
                "d0": None,
                "m0": {
                    "name": "github.com/sg-evals/org-a",
                    "mirrorInfo": {"cloned": True},
                    "head": {"oid": SHA_A},
                    "cutoff": {"oid": SHA_A},
                },
            }

        audits = collect_sourcegraph_audits(
            ["org/a"],
            {"org/a": "github.com/sg-evals/org-a"},
            {"org/a": SHA_A},
            query_api=query_api,
            batch_size=1,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(audits["org/a"]["direct"]["state"], "not_indexed")
        self.assertEqual(audits["org/a"]["mirror"]["state"], "indexed")
        self.assertEqual(audits["org/a"]["mirror"]["head_oid"], SHA_A)
        self.assertEqual(audits["org/a"]["mirror"]["cutoff_state"], "accessible")

    def test_collects_audited_absence_of_declared_license(self):
        def query_runner(query):
            self.assertIn('owner:"org"', query)
            return {
                "r0": {
                    "isPrivate": False,
                    "isArchived": True,
                    "defaultBranchRef": {"name": "main"},
                    "licenseInfo": None,
                }
            }

        audits = collect_github_audits(
            ["org/a"], query_runner=query_runner, batch_size=1
        )

        self.assertEqual(
            audits["org/a"]["license"],
            {"status": "not_declared", "spdx_id": None, "name": None},
        )
        self.assertEqual(
            audits["org/a"]["access"],
            {"status": "public", "archived": True, "default_branch": "main"},
        )

    def test_merges_canonical_identity_without_losing_roles_or_frames(self):
        target_manifest = {
            "cutoff": CUTOFF,
            "repositories": [
                {
                    "id": "Example/Repo",
                    "snapshot": {"commit": SHA_A, "tree": TREE_A},
                }
            ],
        }
        reference_manifest = {
            "repositories": [
                {
                    "id": "example/repo",
                    "role": "dedicated_validation",
                    "label": "human",
                    "snapshot": {"commit": SHA_A, "tree": TREE_A},
                }
            ]
        }
        controls = {"repos": {"EXAMPLE/REPO": {"policy": [{"file": "POLICY.md"}]}}}
        sourcegraph_audits = index_audits("example/repo")
        sourcegraph_audits["example/repo"]["mirror"][
            "name"
        ] = "github.com/sg-evals/custom-transport-name"

        document = build_index_manifest(
            target_manifest=target_manifest,
            survival_frame={"candidates": []},
            survival_inventory={"cutoff": CUTOFF, "repositories": []},
            reference_manifest=reference_manifest,
            control_evidence=controls,
            existing_forks={
                "example/repo": "github.com/sg-evals/custom-transport-name"
            },
            additional_snapshots={},
            repository_audits=repository_audits("example/repo"),
            sourcegraph_audits=sourcegraph_audits,
            observed_at="2026-07-27T12:00:00Z",
        )

        self.assertEqual(len(document["repositories"]), 1)
        repository = document["repositories"][0]
        self.assertEqual(repository["canonical_repository_id"], "example/repo")
        self.assertEqual(
            repository["roles"],
            [
                "adoption_ai_ban_control_seed",
                "classifier_human_reference",
                "dedicated_validation",
                "prevalence_target",
            ],
        )
        self.assertEqual(
            [frame["path"] for frame in repository["source_frames"]],
            [
                "data/control_evidence.json",
                "study/repositories.v1.json",
                "study/targets.v1.json",
            ],
        )
        self.assertEqual(
            repository["sourcegraph"]["mirror"]["name"],
            "github.com/sg-evals/custom-transport-name",
        )
        self.assertEqual(repository["sourcegraph"]["transport_status"], "ready_mirror")
        self.assertEqual(
            repository["sourcegraph"]["selected_name"],
            "github.com/sg-evals/custom-transport-name",
        )
        self.assertEqual(repository["sourcegraph"]["required_action"], "none")
        self.assertEqual(repository["cutoff_commit"], SHA_A)

    def test_rejects_sourcegraph_audit_for_a_different_mirror(self):
        with self.assertRaisesRegex(IndexManifestError, "audited mirror"):
            build_index_manifest(
                target_manifest={
                    "cutoff": CUTOFF,
                    "repositories": [
                        {
                            "id": "org/repo",
                            "snapshot": {"commit": SHA_A, "tree": TREE_A},
                        }
                    ],
                },
                survival_frame={"candidates": []},
                survival_inventory={"cutoff": CUTOFF, "repositories": []},
                reference_manifest={"repositories": []},
                control_evidence={"repos": {}},
                existing_forks={"org/repo": "github.com/sg-evals/intended-mirror"},
                additional_snapshots={},
                repository_audits=repository_audits("org/repo"),
                sourcegraph_audits=index_audits("org/repo"),
                observed_at="2026-07-27T12:00:00Z",
            )

    def test_rejects_conflicting_cutoff_commits_for_one_canonical_repository(self):
        with self.assertRaisesRegex(IndexManifestError, "conflicting cutoff commits"):
            build_index_manifest(
                target_manifest={
                    "cutoff": CUTOFF,
                    "repositories": [
                        {
                            "id": "org/repo",
                            "snapshot": {"commit": SHA_A, "tree": TREE_A},
                        }
                    ],
                },
                survival_frame={"candidates": []},
                survival_inventory={"cutoff": CUTOFF, "repositories": []},
                reference_manifest={
                    "repositories": [
                        {
                            "id": "org/repo",
                            "role": "reference",
                            "label": "agent",
                            "snapshot": {"commit": SHA_B, "tree": TREE_B},
                        }
                    ]
                },
                control_evidence={"repos": {}},
                existing_forks={},
                additional_snapshots={},
                repository_audits=repository_audits("org/repo"),
                sourcegraph_audits=index_audits("org/repo"),
                observed_at="2026-07-27T12:00:00Z",
            )

    def test_reports_whole_tree_duplicates_without_conflating_repositories(self):
        target_manifest = {
            "cutoff": CUTOFF,
            "repositories": [
                {"id": "org/a", "snapshot": {"commit": SHA_A, "tree": TREE_A}},
                {"id": "org/b", "snapshot": {"commit": SHA_B, "tree": TREE_A}},
            ],
        }
        document = build_index_manifest(
            target_manifest=target_manifest,
            survival_frame={"candidates": []},
            survival_inventory={"cutoff": CUTOFF, "repositories": []},
            reference_manifest={"repositories": []},
            control_evidence={"repos": {}},
            existing_forks={},
            additional_snapshots={},
            repository_audits=repository_audits("org/a", "org/b"),
            sourcegraph_audits=index_audits("org/a", "org/b"),
            observed_at="2026-07-27T12:00:00Z",
        )

        self.assertEqual(
            document["duplicate_checks"]["whole_tree_exact_matches"],
            [{"git_tree_sha1": TREE_A, "repository_ids": ["org/a", "org/b"]}],
        )
        self.assertEqual(len(document["repositories"]), 2)

    def test_preserves_non_github_source_while_using_github_mirror_as_transport(self):
        reference_manifest = {
            "repositories": [
                {
                    "id": "org/repo",
                    "url": "https://codeberg.org/org/repo",
                    "role": "reference",
                    "label": "human",
                    "snapshot": {"commit": SHA_A, "tree": TREE_A},
                }
            ]
        }
        audits = index_audits("org/repo")
        audits["org/repo"]["direct"]["name"] = "codeberg.org/org/repo"

        document = build_index_manifest(
            target_manifest={"cutoff": CUTOFF, "repositories": []},
            survival_frame={"candidates": []},
            survival_inventory={"cutoff": CUTOFF, "repositories": []},
            reference_manifest=reference_manifest,
            control_evidence={"repos": {}},
            existing_forks={},
            additional_snapshots={},
            repository_audits=repository_audits("org/repo"),
            sourcegraph_audits=audits,
            observed_at="2026-07-27T12:00:00Z",
        )

        repository = document["repositories"][0]
        self.assertEqual(
            repository["canonical_source_url"],
            "https://codeberg.org/org/repo",
        )
        self.assertEqual(
            repository["sourcegraph"]["direct"]["name"],
            "codeberg.org/org/repo",
        )
        self.assertEqual(
            repository["sourcegraph"]["mirror"]["name"],
            "github.com/sg-evals/org-repo",
        )

    def test_missing_pin_or_audit_is_a_validation_error(self):
        document = build_index_manifest(
            target_manifest={"cutoff": CUTOFF, "repositories": []},
            survival_frame={"candidates": []},
            survival_inventory={"cutoff": CUTOFF, "repositories": []},
            reference_manifest={"repositories": []},
            control_evidence={"repos": {"org/control": {"policy": []}}},
            existing_forks={},
            additional_snapshots={},
            repository_audits={},
            sourcegraph_audits={},
            observed_at="2026-07-27T12:00:00Z",
        )

        errors = validate_index_manifest(document)

        self.assertTrue(any("cutoff_commit" in error for error in errors))
        self.assertTrue(any("license audit" in error for error in errors))
        self.assertTrue(any("Sourcegraph audit" in error for error in errors))

    def test_validator_rejects_wrong_accessible_revision_and_summary(self):
        document = build_index_manifest(
            target_manifest={
                "cutoff": CUTOFF,
                "repositories": [
                    {"id": "org/a", "snapshot": {"commit": SHA_A, "tree": TREE_A}}
                ],
            },
            survival_frame={"candidates": []},
            survival_inventory={"cutoff": CUTOFF, "repositories": []},
            reference_manifest={"repositories": []},
            control_evidence={"repos": {}},
            existing_forks={},
            additional_snapshots={},
            repository_audits=repository_audits("org/a"),
            sourcegraph_audits=index_audits("org/a"),
            observed_at="2026-07-27T12:00:00Z",
        )
        document["repositories"][0]["sourcegraph"]["mirror"]["cutoff_oid"] = SHA_B
        document["sourcegraph_coverage"]["ready_mirror"] = 2

        errors = validate_index_manifest(document)

        self.assertTrue(any("accessible cutoff_oid" in error for error in errors))
        self.assertIn("sourcegraph_coverage does not match repositories", errors)

    def test_frozen_manifest_proves_complete_survival_frame(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads(
            (root / "study" / "sourcegraph-index-manifest.v3.json").read_text()
        )
        schema = json.loads(
            (root / "study" / "sourcegraph-index-manifest.schema.json").read_text()
        )

        self.assertEqual(validate_index_manifest(document), [])
        jsonschema.Draft202012Validator(schema).validate(document)
        survival = document["coverage"]["study/survival-candidates.v1.json"]
        self.assertEqual(survival["expected"], 126)
        self.assertEqual(survival["included"], 126)
        self.assertEqual(survival["missing_repository_ids"], [])


if __name__ == "__main__":
    unittest.main()
