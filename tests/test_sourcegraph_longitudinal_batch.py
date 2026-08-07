import copy
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_batch import (
    build_batch_plan,
    longitudinal_batch_plan_sha256,
    materialize_execution_unit,
)
from authorship.sourcegraph_longitudinal_batch_cli import main

REPOSITORY_ID = "org/repo"
CUTOFF = "a" * 40


def inventories() -> tuple[dict, dict, dict, dict]:
    candidate = "1" * 64
    git_inventory = {
        "candidate_frame_sha256": candidate,
        "outcomes_consulted": False,
        "repositories": [
            {
                "repository_id": REPOSITORY_ID,
                "status": "pinned",
                "cache_path": "/cache/repo",
                "bundle_sha256": "2" * 64,
                "cutoff_commit": CUTOFF,
                "cutoff_tree": "b" * 40,
            }
        ],
    }
    cohort_inventory = {
        "candidate_frame_sha256": candidate,
        "outcomes_consulted": False,
        "repositories": [
            {
                "repository_id": REPOSITORY_ID,
                "status": "reconstructed",
                "cutoff_commit": CUTOFF,
                "shard_path": "/cohorts/org__repo.jsonl",
                "shard_sha256": "3" * 64,
            }
        ],
    }
    lineage_inventory = {
        "candidate_frame_sha256": candidate,
        "horizons_days": [30, 90, 180, 365],
        "repositories": [
            {
                "repository_id": REPOSITORY_ID,
                "transition_sha256": "4" * 64,
            }
        ],
    }
    index_manifest = {
        "outcomes_consulted": False,
        "repositories": [
            {
                "canonical_repository_id": REPOSITORY_ID,
                "cutoff_commit": CUTOFF,
                "cutoff_tree": "b" * 40,
                "sourcegraph": {
                    "selected_name": "github.com/sg-evals/org-repo",
                },
            }
        ],
    }
    return git_inventory, cohort_inventory, lineage_inventory, index_manifest


def test_batch_plan_binds_frozen_inputs_and_pending_probe_outputs(tmp_path: Path):
    documents = inventories()

    plan = build_batch_plan(
        *documents,
        transition_root=Path("/lineage"),
        probe_root=tmp_path / "probes",
    )

    assert plan["repository_count"] == 1
    assert plan["outcomes_consulted"] is False
    assert plan["longitudinal_batch_plan_sha256"] == longitudinal_batch_plan_sha256(
        plan
    )
    unit = plan["units"][0]
    assert unit["repository"]["sourcegraph_name"] == "github.com/sg-evals/org-repo"
    assert unit["introduction_shard"]["sha256"] == "3" * 64
    assert unit["transition_shard"] == {
        "path": "/lineage/org__repo.jsonl",
        "sha256": "4" * 64,
    }
    assert unit["probe_status"] == "pending_sourcegraph"
    assert unit["probe_manifest_path"].endswith("/org__repo.json")
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "study/sourcegraph-longitudinal-batch-plan.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(plan)


def test_batch_plan_is_deterministic_and_rejects_population_drift(tmp_path: Path):
    documents = inventories()
    forward = build_batch_plan(
        *documents,
        transition_root=Path("/lineage"),
        probe_root=tmp_path,
    )
    reverse_documents = tuple(
        {**document, "repositories": list(reversed(document["repositories"]))}
        for document in documents
    )
    reverse = build_batch_plan(
        *reverse_documents,
        transition_root=Path("/lineage"),
        probe_root=tmp_path,
    )
    assert forward == reverse

    changed = list(copy.deepcopy(documents))
    changed[1]["candidate_frame_sha256"] = "9" * 64
    with pytest.raises(LongitudinalExtractionError, match="candidate frame"):
        build_batch_plan(
            *changed,
            transition_root=Path("/lineage"),
            probe_root=tmp_path,
        )


def test_probe_manifest_materializes_content_bound_execution_unit(tmp_path: Path):
    plan = build_batch_plan(
        *inventories(),
        transition_root=Path("/lineage"),
        probe_root=tmp_path,
    )
    manifest = {
        "canonical_repository_id": REPOSITORY_ID,
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": CUTOFF,
        "probe_manifest_sha256": "5" * 64,
    }

    unit = materialize_execution_unit(plan["units"][0], manifest)

    assert unit["sourcegraph_result_manifest_sha256s"] == ["5" * 64]
    assert len(unit["unit_id"]) == 64
    changed = copy.deepcopy(manifest)
    changed["cutoff_commit"] = "c" * 40
    with pytest.raises(LongitudinalExtractionError, match="does not match"):
        materialize_execution_unit(plan["units"][0], changed)
    with pytest.raises(LongitudinalExtractionError, match="planned repository"):
        materialize_execution_unit({}, manifest)
    changed = copy.deepcopy(manifest)
    changed["probe_manifest_sha256"] = "invalid"
    with pytest.raises(LongitudinalExtractionError, match="checksum"):
        materialize_execution_unit(plan["units"][0], changed)


@pytest.mark.parametrize(
    ("document_index", "mutation", "message"),
    [
        (2, lambda document: document.update(horizons_days=[30]), "horizons"),
        (0, lambda document: document.update(outcomes_consulted=True), "outcome blind"),
        (1, lambda document: document.update(repositories=[]), "missing from an input"),
        (0, lambda document: document.update(repositories=None), "must be a list"),
    ],
)
def test_batch_plan_rejects_invalid_frozen_inputs(
    tmp_path: Path, document_index: int, mutation, message: str
):
    documents = list(copy.deepcopy(inventories()))
    mutation(documents[document_index])

    with pytest.raises(LongitudinalExtractionError, match=message):
        build_batch_plan(
            *documents,
            transition_root=Path("/lineage"),
            probe_root=tmp_path,
        )


def test_batch_cli_runs_end_to_end_on_temporary_inventories(tmp_path: Path):
    paths = []
    for index, document in enumerate(inventories()):
        path = tmp_path / f"input-{index}.json"
        path.write_text(json.dumps(document))
        paths.append(path)
    output = tmp_path / "output/plan.json"

    assert (
        main(
            [
                "--git-inventory",
                str(paths[0]),
                "--cohort-inventory",
                str(paths[1]),
                "--lineage-inventory",
                str(paths[2]),
                "--index-manifest",
                str(paths[3]),
                "--transition-root",
                "/lineage",
                "--probe-root",
                str(tmp_path / "probes"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["repository_count"] == 1
