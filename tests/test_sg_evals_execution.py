import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jsonschema

import authorship.sg_evals_execution as execution
from authorship.sg_evals_execution import (
    ExecutionError,
    build_execution_run_manifest,
    execute_plan,
    execute_repository_action,
    execution_run_manifest_sha256,
    execution_preview,
    select_actions,
    validate_execution_journal,
    validate_execution_run_manifest,
)
from authorship.sg_evals_mirroring import action_plan_sha256

SHA256 = "a" * 64
SHA1 = "b" * 40


def action(
    *,
    repository_id="org/repo",
    kind="fast_forward_sync",
    approval_class="external_mutation",
    force_required=False,
):
    mirror_slug = repository_id.replace("/", "-")
    record = {
        "canonical_repository_id": repository_id,
        "canonical_source_url": f"https://github.com/{repository_id}",
        "mirror_name": f"github.com/sg-evals/{mirror_slug}",
        "mirror_slug": mirror_slug,
        "cutoff_commit": SHA1,
        "source_default_branch": "main",
        "action": kind,
        "approval_class": approval_class,
        "force_required": force_required,
        "reason": "test action",
    }
    if kind == "create_git_mirror":
        return {
            **record,
            "canonical_source_url": "https://codeberg.org/org/repo",
            "source_url": "https://codeberg.org/org/repo",
        }
    command_action = {
        **record,
        "argv": [
            "gh",
            "repo",
            "sync",
            f"sg-evals/{mirror_slug}",
            "--source",
            repository_id,
            "--branch",
            "main",
            *(["--force"] if force_required else []),
        ],
    }
    if kind != "replace_standalone_mirror":
        return command_action
    return {
        **record,
        "destination_default_branch": "main",
        "force_refspec": "+refs/heads/main:refs/heads/main",
    }


def plan(*actions):
    document = {
        "plan_version": 3,
        "status": "dry_run_external_approval_required",
        "observed_at": "2026-07-27T12:00:00Z",
        "outcomes_consulted": False,
        "repository_count": len(actions),
        "action_counts": {
            kind: sum(record["action"] == kind for record in actions)
            for kind in sorted({record["action"] for record in actions})
        },
        "source_artifacts": {
            "index_manifest_canonical_sha256": SHA256,
            "github_audit_sha256": SHA256,
            "license_audit_sha256": SHA256,
        },
        "repositories": list(actions),
    }
    return {**document, "plan_sha256": action_plan_sha256(document)}


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class SgEvalsExecutionTests(unittest.TestCase):
    def test_frozen_post_execution_plan_has_no_pending_external_actions(self):
        root = Path(__file__).resolve().parents[1]
        frozen = json.loads(
            (root / "study" / "sg-evals-action-plan.v3.json").read_text()
        )

        routine = select_actions(frozen, "routine")
        destructive = select_actions(frozen, "destructive")

        self.assertEqual(routine, [])
        self.assertEqual(destructive, [])
        self.assertEqual(
            frozen["action_counts"],
            {"hold_private": 1, "hold_redistribution": 5, "no_action": 296},
        )
        self.assertEqual(execution_preview(frozen, "routine")["action_counts"], {})

    def test_routine_scope_accepts_historical_plan_with_legacy_force_records(self):
        root = Path(__file__).resolve().parents[1]
        historical = json.loads(
            (root / "study" / "sg-evals-action-plan.r2.v3.json").read_text()
        )

        routine = select_actions(historical, "routine")

        self.assertEqual(len(routine), 213)
        with self.assertRaisesRegex(ExecutionError, "force action is not isolated"):
            select_actions(historical, "destructive")

    def test_frozen_run_manifest_is_derived_from_the_approved_plan(self):
        root = Path(__file__).resolve().parents[1]
        frozen_plan = json.loads(
            (root / "study" / "sg-evals-action-plan.v3.json").read_text()
        )
        manifest = json.loads(
            (root / "study" / "sg-evals-execution-preview.v3.json").read_text()
        )
        schema = json.loads(
            (root / "study" / "sg-evals-execution-preview.schema.json").read_text()
        )

        self.assertEqual(manifest, build_execution_run_manifest(frozen_plan))
        self.assertEqual(validate_execution_run_manifest(manifest), [])
        self.assertEqual(
            manifest["manifest_sha256"],
            execution_run_manifest_sha256(manifest),
        )
        jsonschema.Draft202012Validator(schema).validate(manifest)

    def test_execute_plan_requires_exact_confirmed_digest(self):
        document = plan(action())
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "journal.json"

            with self.assertRaisesRegex(ExecutionError, "confirmed plan digest"):
                execute_plan(
                    document,
                    scope="routine",
                    confirmed_plan_sha="0" * 64,
                    journal_path=journal_path,
                    command_runner=lambda argv, cwd: completed(argv),
                )

            self.assertFalse(journal_path.exists())

    def test_cli_defaults_to_a_non_mutating_preview(self):
        document = plan(action())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(document))
            output = io.StringIO()
            arguments = [
                "sg-evals-execution",
                "--plan",
                str(plan_path),
                "--scope",
                "routine",
            ]

            with patch("sys.argv", arguments), redirect_stdout(output):
                execution.main()

        preview = json.loads(output.getvalue())
        self.assertEqual(preview["mode"], "dry_run")
        self.assertEqual(preview["selected_count"], 1)

    def test_successful_actions_are_journaled_and_skipped_on_resume(self):
        document = plan(action())
        invocations = []

        def runner(argv, cwd):
            invocations.append((list(argv), cwd))
            return completed(argv, stdout="synced")

        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "journal.json"
            first = execute_plan(
                document,
                scope="routine",
                confirmed_plan_sha=document["plan_sha256"],
                journal_path=journal_path,
                command_runner=runner,
                clock=lambda: "2026-07-27T12:00:00Z",
            )
            second = execute_plan(
                document,
                scope="routine",
                confirmed_plan_sha=document["plan_sha256"],
                journal_path=journal_path,
                command_runner=runner,
                clock=lambda: "2026-07-27T12:01:00Z",
            )

        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(len(invocations), 1)
        self.assertEqual(validate_execution_journal(second), [])
        self.assertEqual(second["results"][0]["status"], "succeeded")
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "study"
                / "sg-evals-execution-journal.schema.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(second)

    def test_failed_action_is_recorded_and_retried(self):
        document = plan(action())
        attempts = iter([completed([], returncode=1, stderr="boom"), completed([])])

        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "journal.json"
            with self.assertRaisesRegex(ExecutionError, "boom"):
                execute_plan(
                    document,
                    scope="routine",
                    confirmed_plan_sha=document["plan_sha256"],
                    journal_path=journal_path,
                    command_runner=lambda argv, cwd: next(attempts),
                    clock=lambda: "2026-07-27T12:00:00Z",
                )
            failed = json.loads(journal_path.read_text())
            resumed = execute_plan(
                document,
                scope="routine",
                confirmed_plan_sha=document["plan_sha256"],
                journal_path=journal_path,
                command_runner=lambda argv, cwd: next(attempts),
                clock=lambda: "2026-07-27T12:01:00Z",
            )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            [result["status"] for result in resumed["results"]],
            ["failed", "succeeded"],
        )

    def test_subprocess_exception_is_recorded_before_propagation(self):
        document = plan(action())

        def runner(argv, cwd):
            raise subprocess.TimeoutExpired(argv, timeout=30)

        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "journal.json"
            with self.assertRaisesRegex(ExecutionError, "timed out"):
                execute_plan(
                    document,
                    scope="routine",
                    confirmed_plan_sha=document["plan_sha256"],
                    journal_path=journal_path,
                    command_runner=runner,
                    clock=lambda: "2026-07-27T12:00:00Z",
                )
            journal = json.loads(journal_path.read_text())

        self.assertEqual(journal["status"], "failed")
        self.assertIn("timed out", journal["results"][0]["error"])

    def test_non_github_mirror_clones_before_creating_and_mirror_pushes(self):
        record = action(kind="create_git_mirror")
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            if argv[:3] == ["gh", "repo", "view"]:
                return completed(
                    argv,
                    returncode=1,
                    stderr="Could not resolve to a Repository with the name",
                )
            return completed(argv)

        traces = execute_repository_action(record, command_runner=runner)

        self.assertEqual(invocations[0][:3], ["git", "clone", "--bare"])
        self.assertEqual(invocations[1][:3], ["gh", "repo", "view"])
        self.assertEqual(invocations[2][:3], ["gh", "repo", "create"])
        self.assertIn("--mirror", invocations[3])
        self.assertEqual(len(traces), 4)

    def test_audited_cutoff_identical_transport_is_accepted(self):
        record = action(kind="create_git_mirror")
        record["source_url"] = "https://github.com/org/repo"
        record["source_transport_cutoff_oid"] = record["cutoff_commit"]
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            if argv[:3] == ["gh", "repo", "view"]:
                return completed(
                    argv,
                    returncode=1,
                    stderr="Could not resolve to a Repository with the name",
                )
            return completed(argv)

        execute_repository_action(record, command_runner=runner)

        self.assertEqual(
            invocations[0],
            [
                "git",
                "clone",
                "--bare",
                "https://github.com/org/repo",
                invocations[0][-1],
            ],
        )

    def test_existing_wrong_fork_collision_is_not_overwritten(self):
        record = action(
            kind="create_github_fork",
            approval_class="external_mutation",
        )
        record["argv"] = [
            "gh",
            "repo",
            "fork",
            "org/repo",
            "--org",
            "sg-evals",
            "--fork-name",
            "org-repo",
        ]

        def runner(argv, cwd):
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "nameWithOwner": "sg-evals/org-repo",
                        "isFork": True,
                        "parent": {"nameWithOwner": "other/repo"},
                        "description": None,
                    }
                ),
            )

        with self.assertRaisesRegex(ExecutionError, "name collision"):
            execute_repository_action(record, command_runner=runner)

    def test_stored_argv_cannot_inject_an_unapproved_command(self):
        record = action()
        record["argv"] = ["bash", "-lc", "arbitrary command"]
        invocations = []

        with self.assertRaisesRegex(ExecutionError, "does not match"):
            execute_repository_action(
                record,
                command_runner=lambda argv, cwd: invocations.append(list(argv)),
            )

        self.assertEqual(invocations, [])

    def test_unsafe_mirror_slug_is_rejected_before_any_command(self):
        record = action()
        record["mirror_slug"] = "../escape"
        record["mirror_name"] = "github.com/sg-evals/../escape"
        invocations = []

        with self.assertRaisesRegex(ExecutionError, "unsafe mirror slug"):
            execute_repository_action(
                record,
                command_runner=lambda argv, cwd: invocations.append(list(argv)),
            )

        self.assertEqual(invocations, [])

    def test_force_action_cannot_be_relabeled_as_routine(self):
        record = action(
            kind="replace_standalone_mirror",
            approval_class="external_mutation",
            force_required=False,
        )
        document = plan(record)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExecutionError, "approval metadata"):
                execute_plan(
                    document,
                    scope="routine",
                    confirmed_plan_sha=document["plan_sha256"],
                    journal_path=Path(directory) / "journal.json",
                    command_runner=lambda argv, cwd: completed(argv),
                    clock=lambda: "2026-07-27T12:00:00Z",
                )

    def test_standalone_replacement_force_pushes_one_explicit_refspec(self):
        record = action(
            kind="replace_standalone_mirror",
            approval_class="destructive_external",
            force_required=True,
        )
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            return completed(argv)

        traces = execute_repository_action(record, command_runner=runner)

        self.assertEqual(
            invocations[0][:7],
            [
                "git",
                "clone",
                "--bare",
                "--single-branch",
                "--branch",
                "main",
                "https://github.com/org/repo",
            ],
        )
        self.assertEqual(
            invocations[1][-2:],
            [
                "https://github.com/sg-evals/org-repo.git",
                "+refs/heads/main:refs/heads/main",
            ],
        )
        self.assertEqual(len(traces), 2)

    def test_legacy_sync_resolves_and_validates_upstream_default_branch(self):
        record = action()
        record.pop("source_default_branch")
        record["argv"] = [
            "gh",
            "repo",
            "sync",
            "sg-evals/org-repo",
            "--source",
            "org/repo",
        ]
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            if argv[2] == "view":
                return completed(
                    argv,
                    stdout=json.dumps({"defaultBranchRef": {"name": "canary"}}),
                )
            return completed(argv)

        traces = execute_repository_action(record, command_runner=runner)

        self.assertEqual(
            invocations,
            [
                [
                    "gh",
                    "repo",
                    "view",
                    "org/repo",
                    "--json",
                    "defaultBranchRef",
                ],
                [
                    "gh",
                    "repo",
                    "sync",
                    "sg-evals/org-repo",
                    "--source",
                    "org/repo",
                    "--branch",
                    "canary",
                ],
            ],
        )
        self.assertEqual(len(traces), 2)

    def test_existing_intended_fork_uses_actual_gh_parent_shape(self):
        record = action(
            kind="create_github_fork",
            approval_class="external_mutation",
        )
        record["argv"] = [
            "gh",
            "repo",
            "fork",
            "org/repo",
            "--org",
            "sg-evals",
            "--fork-name",
            "org-repo",
        ]
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "nameWithOwner": "sg-evals/org-repo",
                        "isFork": True,
                        "parent": {
                            "name": "repo",
                            "owner": {"login": "org"},
                        },
                        "description": None,
                    }
                ),
            )

        traces = execute_repository_action(record, command_runner=runner)

        self.assertEqual(len(invocations), 1)
        self.assertEqual(len(traces), 1)

    def test_existing_fork_accepts_a_resolved_upstream_rename(self):
        record = action(
            repository_id="beehiveinnovations/zen-mcp-server",
            kind="create_github_fork",
            approval_class="external_mutation",
        )
        record["argv"] = [
            "gh",
            "repo",
            "fork",
            "beehiveinnovations/zen-mcp-server",
            "--org",
            "sg-evals",
            "--fork-name",
            "beehiveinnovations-zen-mcp-server",
        ]
        invocations = []

        def runner(argv, cwd):
            invocations.append(list(argv))
            if argv[2] == "view" and argv[3].startswith("sg-evals/"):
                payload = {
                    "nameWithOwner": "sg-evals/beehiveinnovations-zen-mcp-server",
                    "isFork": True,
                    "parent": {
                        "name": "pal-mcp-server",
                        "owner": {"login": "BeehiveInnovations"},
                    },
                    "description": None,
                }
            else:
                payload = {"nameWithOwner": "BeehiveInnovations/pal-mcp-server"}
            return completed(argv, stdout=json.dumps(payload))

        traces = execute_repository_action(record, command_runner=runner)

        self.assertEqual(len(invocations), 2)
        self.assertEqual(len(traces), 2)


if __name__ == "__main__":
    unittest.main()
