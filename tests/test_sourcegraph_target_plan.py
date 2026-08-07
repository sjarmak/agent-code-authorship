from copy import deepcopy

from authorship.sourcegraph_target_plan import (
    build_target_unit_plan,
    target_unit_plan_sha256,
    validate_target_unit_plan,
)


def _targets():
    return {
        "manifest_version": 1,
        "repositories": [
            {
                "id": "acme/alpha",
                "role": "target",
                "label": "unlabeled",
                "languages": ["Python"],
                "snapshot": {
                    "commit": "a" * 40,
                    "tree": "b" * 40,
                    "committed_at": "2026-01-02T00:00:00Z",
                },
                "effective_date_range": [
                    "2024-01-01T00:00:00Z",
                    "2026-01-02T23:59:59Z",
                ],
                "excluded_paths": ["vendored", "generated"],
            },
            {
                "id": "acme/beta",
                "role": "target",
                "label": "unlabeled",
                "languages": ["Go"],
                "snapshot": {
                    "commit": "c" * 40,
                    "tree": "d" * 40,
                    "committed_at": "2026-01-03T00:00:00Z",
                },
                "effective_date_range": [
                    "2024-01-01T00:00:00Z",
                    "2026-01-03T23:59:59Z",
                ],
                "excluded_paths": ["vendored", "generated"],
            },
        ],
    }


def _index():
    return {
        "manifest_version": 3,
        "repositories": [
            {
                "canonical_repository_id": "acme/alpha",
                "cutoff_commit": "a" * 40,
                "cutoff_tree": "b" * 40,
                "roles": ["prevalence_target"],
                "sourcegraph": {
                    "selected_name": "github.com/sg-evals/acme-alpha",
                    "transport_status": "ready_mirror",
                },
            },
            {
                "canonical_repository_id": "acme/beta",
                "cutoff_commit": "c" * 40,
                "cutoff_tree": "d" * 40,
                "roles": ["prevalence_target"],
                "sourcegraph": {
                    "selected_name": "github.com/sg-evals/acme-beta",
                    "transport_status": "ready_mirror",
                },
            },
        ],
    }


def _features():
    return {"schema_version": 1, "names": ["log_lines"], "languages": ["Python", "Go"]}


def _tree_fetcher(name, revision):
    if name.endswith("alpha"):
        return {
            "oid": revision,
            "tree_oid": "b" * 40,
            "paths": [
                "src/main.py",
                "tests/test_main.py",
                "vendor/ignored.py",
                "src/ignored.go",
                "README.md",
            ],
        }
    return {
        "oid": revision,
        "tree_oid": "d" * 40,
        "paths": ["cmd/main.go", "generated/client.go", "script.py"],
    }


def test_plan_freezes_every_eligible_file_at_exact_target_snapshot():
    plan = build_target_unit_plan(_targets(), _index(), _features(), _tree_fetcher)

    assert plan["status"] == "frozen_before_target_outcome_extraction"
    assert plan["repository_count"] == 2
    assert plan["file_count"] == 3
    assert plan["pending_repositories"] == []
    assert plan["repositories"] == [
        {
            "repository_id": "acme/alpha",
            "sourcegraph_name": "github.com/sg-evals/acme-alpha",
            "cutoff_commit": "a" * 40,
            "cutoff_tree": "b" * 40,
            "eligible_file_count": 2,
            "file_count": 2,
            "file_inclusion_probability": 1.0,
            "file_sampling_weight": 1.0,
        },
        {
            "repository_id": "acme/beta",
            "sourcegraph_name": "github.com/sg-evals/acme-beta",
            "cutoff_commit": "c" * 40,
            "cutoff_tree": "d" * 40,
            "eligible_file_count": 1,
            "file_count": 1,
            "file_inclusion_probability": 1.0,
            "file_sampling_weight": 1.0,
        },
    ]
    assert [
        (task["repository_id"], task["path"], task["language"])
        for task in plan["tasks"]
    ] == [
        ("acme/alpha", "src/main.py", "Python"),
        ("acme/alpha", "tests/test_main.py", "Python"),
        ("acme/beta", "cmd/main.go", "Go"),
    ]
    assert (
        validate_target_unit_plan(
            plan, _targets(), _index(), _features(), _tree_fetcher
        )
        == []
    )


def test_plan_is_blocked_when_a_fixed_target_revision_is_not_indexed():
    def unavailable(name, revision):
        if name.endswith("beta"):
            raise RuntimeError("revision not found")
        return _tree_fetcher(name, revision)

    plan = build_target_unit_plan(_targets(), _index(), _features(), unavailable)

    assert plan["status"] == "blocked_missing_indexed_revision"
    assert plan["ready_repository_count"] == 1
    assert plan["pending_repositories"] == [
        {
            "repository_id": "acme/beta",
            "sourcegraph_name": "github.com/sg-evals/acme-beta",
            "cutoff_commit": "c" * 40,
            "reason": "revision not found",
        }
    ]


def test_validator_reenumerates_sourcegraph_and_rejects_rehashed_omission():
    plan = build_target_unit_plan(_targets(), _index(), _features(), _tree_fetcher)
    forged = deepcopy(plan)
    forged["tasks"] = forged["tasks"][:-1]
    forged["file_count"] -= 1
    forged["target_unit_plan_sha256"] = target_unit_plan_sha256(forged)

    errors = validate_target_unit_plan(
        forged, _targets(), _index(), _features(), _tree_fetcher
    )

    assert any("independent Sourcegraph enumeration" in error for error in errors)


def test_plan_rejects_snapshot_tree_drift():
    index = _index()
    index["repositories"][0]["cutoff_tree"] = "e" * 40

    plan = build_target_unit_plan(_targets(), index, _features(), _tree_fetcher)

    assert plan["status"] == "blocked_missing_indexed_revision"
    assert (
        plan["pending_repositories"][0]["reason"] == "index manifest snapshot mismatch"
    )


def test_plan_matches_canonical_repository_ids_case_insensitively():
    targets = _targets()
    targets["repositories"][0]["id"] = "Acme/Alpha"

    plan = build_target_unit_plan(targets, _index(), _features(), _tree_fetcher)

    assert plan["ready_repository_count"] == 2
    assert plan["repositories"][0]["repository_id"] == "Acme/Alpha"


def test_plan_uses_outcome_blind_uniform_file_sampling_with_known_probability():
    paths = [f"src/file_{number:03d}.py" for number in range(60)]

    def many_files(_name, revision):
        return {"oid": revision, "tree_oid": "b" * 40, "paths": paths}

    targets = _targets()
    targets["repositories"] = targets["repositories"][:1]
    index = _index()
    index["repositories"] = index["repositories"][:1]
    first = build_target_unit_plan(targets, index, _features(), many_files)
    second = build_target_unit_plan(targets, index, _features(), many_files)

    assert first == second
    assert first["eligible_file_count"] == 60
    assert first["file_count"] == 50
    assert first["repositories"][0]["file_inclusion_probability"] == 50 / 60
    assert first["repositories"][0]["file_sampling_weight"] == 60 / 50
    assert all(task["file_sampling_weight"] == 60 / 50 for task in first["tasks"])
