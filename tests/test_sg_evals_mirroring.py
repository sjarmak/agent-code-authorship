import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

import authorship.sg_evals_mirroring as mirroring
from authorship.sg_evals_mirroring import (
    action_plan_sha256,
    build_action_plan,
    build_license_audit,
    classify_repository_action,
    collect_github_mirror_audit,
    github_audit_sha256,
    license_audit_sha256,
    validate_github_audit,
    validate_license_audit,
    validate_action_plan,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def repository(
    *,
    repository_id="org/repo",
    source_url="https://github.com/org/repo",
    transport_status="mirror_creation_required",
    required_action="create_and_index_mirror",
    access_status="public",
    license_status="declared",
    spdx_id="MIT",
    default_branch="main",
):
    mirror_name = f"github.com/sg-evals/{repository_id.replace('/', '-')}"
    return {
        "canonical_repository_id": repository_id,
        "canonical_source_url": source_url,
        "cutoff_commit": SHA_A,
        "access": {"status": access_status, "default_branch": default_branch},
        "license": {"status": license_status, "spdx_id": spdx_id},
        "sourcegraph": {
            "transport_status": transport_status,
            "required_action": required_action,
            "selected_name": mirror_name,
            "mirror": {"name": mirror_name},
        },
    }


def github_state(
    *,
    exists=False,
    is_fork=None,
    parent=None,
    cutoff_oid=None,
    default_branch="main",
):
    return {
        "exists": exists,
        "is_fork": is_fork,
        "parent": parent,
        "visibility": "PUBLIC" if exists else None,
        "default_branch": default_branch if exists else None,
        "head_oid": SHA_B if exists else None,
        "cutoff_oid": cutoff_oid,
    }


def license_evidence(redistribution_class="open_source"):
    return {"redistribution_class": redistribution_class}


class SgEvalsMirroringTests(unittest.TestCase):
    def test_collects_batched_read_only_github_state_with_digest(self):
        index_manifest = {
            "repositories": [
                repository(repository_id="org/a"),
                repository(repository_id="org/b"),
            ]
        }
        responses = iter(
            [
                {"r0": None},
                {
                    "r0": {
                        "isFork": True,
                        "visibility": "PUBLIC",
                        "parent": {"nameWithOwner": "org/b"},
                        "defaultBranchRef": {
                            "name": "main",
                            "target": {"oid": SHA_B},
                        },
                        "cutoff": {"oid": SHA_A},
                    }
                },
            ]
        )

        audit = collect_github_mirror_audit(
            index_manifest,
            observed_at="2026-07-27T12:00:00Z",
            query_runner=lambda query: next(responses),
            batch_size=1,
        )

        self.assertEqual(validate_github_audit(audit), [])
        self.assertFalse(audit["repositories"][0]["exists"])
        self.assertEqual(audit["repositories"][1]["parent"], "org/b")
        self.assertEqual(audit["repositories"][1]["cutoff_oid"], SHA_A)

    def test_cli_writes_all_three_dry_run_artifacts(self):
        index_manifest = {"repositories": [repository()]}
        github_audit = collect_github_mirror_audit(
            index_manifest,
            observed_at="2026-07-27T12:00:00Z",
            query_runner=lambda query: {"r0": None},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index_path = root / "index.json"
            override_path = root / "overrides.json"
            license_path = root / "licenses.json"
            github_path = root / "github.json"
            plan_path = root / "plan.json"
            index_path.write_text(json.dumps(index_manifest))
            override_path.write_text(json.dumps({"overrides": []}))
            arguments = [
                "sg-evals-mirroring",
                "--index-manifest",
                str(index_path),
                "--license-overrides",
                str(override_path),
                "--license-audit-output",
                str(license_path),
                "--github-audit-output",
                str(github_path),
                "--plan-output",
                str(plan_path),
            ]

            with (
                patch("sys.argv", arguments),
                patch.object(
                    mirroring,
                    "collect_github_mirror_audit",
                    return_value=github_audit,
                ),
            ):
                mirroring.main()

            self.assertTrue(license_path.exists())
            self.assertTrue(github_path.exists())
            self.assertEqual(
                json.loads(plan_path.read_text())["action_counts"],
                {"create_github_fork": 1},
            )

    def test_license_audit_uses_frozen_metadata_and_explicit_overrides(self):
        index_manifest = {
            "repositories": [
                repository(repository_id="org/declared"),
                repository(
                    repository_id="org/missing",
                    license_status="not_declared",
                ),
            ]
        }
        overrides = {
            "overrides": [
                {
                    "canonical_repository_id": "org/missing",
                    "redistribution_class": "proprietary",
                    "license_expression": "Proprietary",
                    "evidence": {
                        "kind": "license_file",
                        "url": "https://example.test/LICENSE",
                        "sha256": "c" * 64,
                        "summary": "All rights reserved.",
                    },
                }
            ]
        }

        audit = build_license_audit(
            index_manifest,
            overrides,
            observed_at="2026-07-27T12:00:00Z",
        )

        self.assertEqual(validate_license_audit(audit), [])
        self.assertEqual(audit["audit_sha256"], license_audit_sha256(audit))
        self.assertEqual(audit["records"][0]["redistribution_class"], "open_source")
        self.assertEqual(audit["records"][1]["redistribution_class"], "proprietary")

    def test_license_audit_holds_unrecognized_declared_license_metadata(self):
        index_manifest = {
            "repositories": [
                repository(repository_id="org/other", spdx_id="NOASSERTION"),
                repository(repository_id="org/none", spdx_id=None),
            ]
        }

        audit = build_license_audit(
            index_manifest,
            {"overrides": []},
            observed_at="2026-07-27T12:00:00Z",
        )

        self.assertEqual(
            [record["redistribution_class"] for record in audit["records"]],
            ["no_declared_license", "no_declared_license"],
        )

    def test_license_audit_rejects_unknown_or_duplicate_overrides(self):
        index_manifest = {"repositories": [repository()]}
        duplicate = {
            "overrides": [
                {"canonical_repository_id": "org/repo"},
                {"canonical_repository_id": "org/repo"},
            ]
        }
        unknown = {"overrides": [{"canonical_repository_id": "other/repo"}]}

        with self.assertRaisesRegex(ValueError, "duplicate license override"):
            build_license_audit(index_manifest, duplicate, observed_at="frozen")
        with self.assertRaisesRegex(ValueError, "unknown repository"):
            build_license_audit(index_manifest, unknown, observed_at="frozen")

    def test_license_audit_validator_rejects_unsafe_open_source_classification(self):
        audit = build_license_audit(
            {
                "repositories": [
                    repository(repository_id="org/repo", spdx_id="NOASSERTION")
                ]
            },
            {"overrides": []},
            observed_at="frozen",
        )
        unsafe = {
            **audit,
            "redistribution_counts": {"open_source": 1},
            "records": [
                {
                    **audit["records"][0],
                    "redistribution_class": "open_source",
                }
            ],
        }
        unsafe["audit_sha256"] = license_audit_sha256(unsafe)

        self.assertIn(
            "open_source records require recognized SPDX evidence or an override",
            validate_license_audit(unsafe),
        )

    def test_classifies_only_correct_fork_as_fast_forward_sync(self):
        record = repository(
            transport_status="mirror_revision_sync_required",
            required_action="sync_mirror_to_cutoff",
        )

        action = classify_repository_action(
            record,
            github_state(
                exists=True,
                is_fork=True,
                parent="org/repo",
                cutoff_oid=SHA_A,
            ),
            license_evidence(),
        )

        self.assertEqual(action["action"], "fast_forward_sync")
        self.assertFalse(action["force_required"])
        self.assertEqual(
            action["argv"],
            [
                "gh",
                "repo",
                "sync",
                "sg-evals/org-repo",
                "--source",
                "org/repo",
                "--branch",
                "main",
            ],
        )

    def test_standalone_collision_requires_separate_force_approval(self):
        record = repository(
            transport_status="mirror_revision_sync_required",
            required_action="sync_mirror_to_cutoff",
        )

        action = classify_repository_action(
            record,
            github_state(exists=True, is_fork=False),
            license_evidence(),
        )

        self.assertEqual(action["action"], "replace_standalone_mirror")
        self.assertTrue(action["force_required"])
        self.assertEqual(action["approval_class"], "destructive_external")
        self.assertEqual(action["source_default_branch"], "main")
        self.assertEqual(action["destination_default_branch"], "main")
        self.assertEqual(
            action["force_refspec"],
            "+refs/heads/main:refs/heads/main",
        )
        self.assertNotIn("argv", action)

    def test_standalone_replacement_maps_source_to_destination_default_branch(self):
        record = repository(
            transport_status="mirror_revision_sync_required",
            required_action="sync_mirror_to_cutoff",
            default_branch="master",
        )

        action = classify_repository_action(
            record,
            github_state(exists=True, is_fork=False, default_branch="main"),
            license_evidence(),
        )

        self.assertEqual(action["source_default_branch"], "master")
        self.assertEqual(action["destination_default_branch"], "main")
        self.assertEqual(
            action["force_refspec"],
            "+refs/heads/master:refs/heads/main",
        )

    def test_sync_is_held_when_cutoff_commit_is_not_in_fork_network(self):
        record = repository(
            transport_status="mirror_revision_sync_required",
            required_action="sync_mirror_to_cutoff",
        )

        action = classify_repository_action(
            record,
            github_state(exists=True, is_fork=True, parent="org/repo"),
            license_evidence(),
        )

        self.assertEqual(action["action"], "hold_source_revision_missing")
        self.assertEqual(action["approval_class"], "additional_approval")

    def test_missing_github_source_uses_named_fork(self):
        action = classify_repository_action(
            repository(),
            github_state(),
            license_evidence(),
        )

        self.assertEqual(action["action"], "create_github_fork")
        self.assertEqual(
            action["argv"],
            [
                "gh",
                "repo",
                "fork",
                "org/repo",
                "--org",
                "sg-evals",
                "--fork-name",
                "org-repo",
            ],
        )

    def test_non_github_source_requires_full_git_mirror(self):
        action = classify_repository_action(
            repository(source_url="https://codeberg.org/org/repo"),
            github_state(),
            license_evidence(),
        )

        self.assertEqual(action["action"], "create_git_mirror")
        self.assertEqual(action["source_url"], "https://codeberg.org/org/repo")
        self.assertNotIn("argv", action)

    def test_non_github_source_can_use_an_audited_cutoff_identical_transport(self):
        evidence = {
            "redistribution_class": "open_source",
            "mirror_transport_url": "https://github.com/org/repo",
            "mirror_transport_cutoff_oid": SHA_A,
        }

        action = classify_repository_action(
            repository(source_url="https://unavailable.example/org/repo"),
            github_state(),
            evidence,
        )

        self.assertEqual(
            action["canonical_source_url"], "https://unavailable.example/org/repo"
        )
        self.assertEqual(action["source_url"], "https://github.com/org/repo")
        self.assertEqual(action["source_transport_cutoff_oid"], SHA_A)

    def test_private_and_redistribution_exceptions_are_held(self):
        private = classify_repository_action(
            repository(access_status="private"),
            github_state(),
            license_evidence(),
        )
        no_license = classify_repository_action(
            repository(),
            github_state(),
            license_evidence("no_declared_license"),
        )
        proprietary = classify_repository_action(
            repository(),
            github_state(),
            license_evidence("proprietary"),
        )

        self.assertEqual(private["action"], "hold_private")
        self.assertEqual(no_license["action"], "hold_redistribution")
        self.assertEqual(proprietary["action"], "hold_redistribution")

    def test_frozen_action_plan_matches_live_read_only_audits(self):
        root = Path(__file__).resolve().parents[1]
        plan = json.loads((root / "study" / "sg-evals-action-plan.v3.json").read_text())
        schema = json.loads(
            (root / "study" / "sg-evals-action-plan.schema.json").read_text()
        )

        self.assertEqual(validate_action_plan(plan), [])
        self.assertEqual(plan["plan_sha256"], action_plan_sha256(plan))
        jsonschema.Draft202012Validator(schema).validate(plan)
        self.assertEqual(
            set(plan["source_artifacts"]),
            {
                "index_manifest_canonical_sha256",
                "github_audit_sha256",
                "license_audit_sha256",
            },
        )
        self.assertEqual(plan["repository_count"], 302)
        self.assertEqual(
            plan["action_counts"],
            {
                "hold_private": 1,
                "hold_redistribution": 5,
                "no_action": 296,
            },
        )
        license_audit = json.loads(
            (root / "study" / "repository-license-audit.v3.json").read_text()
        )
        license_schema = json.loads(
            (root / "study" / "repository-license-audit.schema.json").read_text()
        )
        self.assertEqual(validate_license_audit(license_audit), [])
        jsonschema.Draft202012Validator(license_schema).validate(license_audit)
        self.assertEqual(license_audit["repository_count"], 302)
        github_audit = json.loads(
            (root / "study" / "sg-evals-github-audit.v3.json").read_text()
        )
        github_schema = json.loads(
            (root / "study" / "sg-evals-github-audit.schema.json").read_text()
        )
        self.assertEqual(validate_github_audit(github_audit), [])
        self.assertEqual(
            github_audit["audit_sha256"], github_audit_sha256(github_audit)
        )
        jsonschema.Draft202012Validator(github_schema).validate(github_audit)

    def test_plan_builder_preserves_one_record_per_canonical_repository(self):
        index_manifest = {
            "repositories": [
                repository(repository_id="org/a"),
                repository(repository_id="org/b"),
            ]
        }
        github_audit = {
            "repositories": [
                {
                    "canonical_repository_id": "org/a",
                    **github_state(),
                },
                {
                    "canonical_repository_id": "org/b",
                    **github_state(),
                },
            ]
        }
        license_audit = {
            "records": [
                {
                    "canonical_repository_id": "org/a",
                    **license_evidence(),
                },
                {
                    "canonical_repository_id": "org/b",
                    **license_evidence(),
                },
            ]
        }

        plan = build_action_plan(
            index_manifest,
            github_audit,
            license_audit,
            observed_at="2026-07-27T12:00:00Z",
        )

        self.assertEqual(plan["repository_count"], 2)
        self.assertEqual(
            [record["canonical_repository_id"] for record in plan["repositories"]],
            ["org/a", "org/b"],
        )


if __name__ == "__main__":
    unittest.main()
