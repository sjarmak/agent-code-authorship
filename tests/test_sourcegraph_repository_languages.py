import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_repository_languages_cli as language_cli
from authorship.sourcegraph_adoption_review import adoption_review_tranche_sha256
from authorship.sourcegraph_repository_languages import (
    RepositoryLanguageError,
    build_repository_language_inventory,
    repository_language_inventory_sha256,
    sg_evals_repository_names,
)


def _tranche() -> dict:
    tasks = [
        {
            "canonical_repository_id": repository_id,
            "sourcegraph_name": sourcegraph_name,
        }
        for repository_id, sourcegraph_name in [
            ("org/alpha", "github.com/sg-evals/org-alpha"),
            ("org/beta", "github.com/org/beta"),
            ("org/gamma", "github.com/sg-evals/org-gamma"),
        ]
    ]
    return {
        "tranche_number": 1,
        "tranche_sha256": "a" * 64,
        "case_index_sha256": "b" * 64,
        "outcomes_consulted": False,
        "tasks": tasks,
    }


def _sg_evals_names() -> dict[str, str]:
    return {
        repository_id: f"github.com/sg-evals/{repository_id.replace('/', '-')}"
        for repository_id in ["org/alpha", "org/beta", "org/gamma"]
    }


def _manifest() -> dict:
    return {
        "outcomes_consulted": False,
        "repositories": [
            {
                "canonical_repository_id": repository_id,
                "sourcegraph": {
                    "mirror": {
                        "name": sourcegraph_name,
                        "state": "indexed",
                    }
                },
            }
            for repository_id, sourcegraph_name in _sg_evals_names().items()
        ],
    }


def test_inventory_queries_frozen_repositories_in_batches():
    calls = []
    languages = {
        "github.com/sg-evals/org-alpha": "Go",
        "github.com/sg-evals/org-beta": "Python",
        "github.com/sg-evals/org-gamma": "Go",
    }

    def query_api(query: str, **variables: str) -> dict:
        calls.append((query, variables))
        return {
            alias: {"name": name, "language": languages[name]}
            for alias, name in (
                (f"repository{index}", variables[f"name{index}"])
                for index in range(len(variables))
            )
        }

    inventory = build_repository_language_inventory(
        _tranche(),
        query_api,
        sourcegraph_names=_sg_evals_names(),
        index_manifest_sha256="d" * 64,
        observed_at="2026-07-29T19:30:00Z",
        batch_size=2,
    )

    assert len(calls) == 2
    assert "$name0: String!" in calls[0][0]
    assert "repository0: repository(name: $name0)" in calls[0][0]
    assert calls[0][1] == {
        "name0": "github.com/sg-evals/org-alpha",
        "name1": "github.com/sg-evals/org-beta",
    }
    assert inventory["repository_count"] == 3
    assert inventory["language_counts"] == {"Go": 2, "Python": 1}
    assert inventory["repositories"][0] == {
        "canonical_repository_id": "org/alpha",
        "review_sourcegraph_name": "github.com/sg-evals/org-alpha",
        "language_sourcegraph_name": "github.com/sg-evals/org-alpha",
        "language": "Go",
    }
    assert inventory["sourcegraph_capability"] == "Repository.language"
    assert inventory["index_manifest_sha256"] == "d" * 64
    assert inventory["sourcegraph_scope"] == "github.com/sg-evals/*"
    assert inventory["review_scope_exception_count"] == 1
    assert inventory["scip_required"] is False
    assert inventory["outcomes_consulted"] is False
    assert inventory["inventory_sha256"] == repository_language_inventory_sha256(
        inventory
    )
    schema_path = (
        Path(__file__).parents[1]
        / "study"
        / "sourcegraph-repository-language-inventory.schema.json"
    )
    jsonschema.validate(inventory, json.loads(schema_path.read_text()))


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda tranche: tranche["tasks"].append(copy.deepcopy(tranche["tasks"][0])),
            "duplicate canonical repository",
        ),
        (
            lambda tranche: tranche.update(outcomes_consulted=True),
            "outcomes",
        ),
        (
            lambda tranche: tranche.update(tranche_number=2),
            "first tranche",
        ),
    ],
)
def test_inventory_rejects_unfrozen_or_duplicate_input(mutator, message):
    tranche = _tranche()
    mutator(tranche)

    with pytest.raises(RepositoryLanguageError, match=message):
        build_repository_language_inventory(
            tranche,
            lambda *_args, **_kwargs: {},
            sourcegraph_names=_sg_evals_names(),
            index_manifest_sha256="d" * 64,
            observed_at="2026-07-29T19:30:00Z",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"repository0": None}, "missing repository"),
        (
            {
                "repository0": {
                    "name": "github.com/sg-evals/wrong-repository",
                    "language": "Go",
                }
            },
            "identity mismatch",
        ),
        (
            {
                "repository0": {
                    "name": "github.com/sg-evals/org-alpha",
                    "language": None,
                }
            },
            "missing language",
        ),
    ],
)
def test_inventory_fails_closed_on_sourcegraph_metadata(payload, message):
    tranche = _tranche()
    tranche["tasks"] = tranche["tasks"][:1]
    sourcegraph_names = {"org/alpha": "github.com/sg-evals/org-alpha"}

    with pytest.raises(RepositoryLanguageError, match=message):
        build_repository_language_inventory(
            tranche,
            lambda *_args, **_kwargs: payload,
            sourcegraph_names=sourcegraph_names,
            index_manifest_sha256="d" * 64,
            observed_at="2026-07-29T19:30:00Z",
        )


def test_inventory_rejects_invalid_batch_size_and_observation_time():
    with pytest.raises(RepositoryLanguageError, match="batch size"):
        build_repository_language_inventory(
            _tranche(),
            lambda *_args, **_kwargs: {},
            sourcegraph_names=_sg_evals_names(),
            index_manifest_sha256="d" * 64,
            observed_at="2026-07-29T19:30:00Z",
            batch_size=0,
        )
    with pytest.raises(RepositoryLanguageError, match="observed_at"):
        build_repository_language_inventory(
            _tranche(),
            lambda *_args, **_kwargs: {},
            sourcegraph_names=_sg_evals_names(),
            index_manifest_sha256="d" * 64,
            observed_at="not-a-timestamp",
        )


def test_sg_evals_name_map_requires_one_indexed_mirror_per_repository():
    manifest = _manifest()

    assert sg_evals_repository_names(manifest) == _sg_evals_names()
    manifest["repositories"].append(
        {
            "canonical_repository_id": "excluded/repository",
            "sourcegraph": {
                "mirror": {
                    "name": "github.com/sg-evals/excluded-repository",
                    "state": "not_indexed",
                }
            },
        }
    )
    assert (
        sg_evals_repository_names(
            manifest,
            repository_ids=set(_sg_evals_names()),
        )
        == _sg_evals_names()
    )
    manifest["repositories"][0]["sourcegraph"]["mirror"]["state"] = "not_indexed"
    with pytest.raises(RepositoryLanguageError, match="indexed sg-evals mirror"):
        sg_evals_repository_names(manifest)


def test_cli_pins_source_tranche_and_writes_inventory(monkeypatch, tmp_path):
    tranche = _tranche()
    tranche["tranche_sha256"] = adoption_review_tranche_sha256(tranche)
    tranche_path = tmp_path / "tranche.json"
    manifest_path = tmp_path / "index-manifest.json"
    output_path = tmp_path / "languages.json"
    tranche_path.write_text(json.dumps(tranche))
    manifest_path.write_text(json.dumps(_manifest()))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def query_api(_query: str, **variables: str) -> dict:
        return {
            f"repository{index}": {"name": name, "language": "Go"}
            for index, name in enumerate(variables.values())
        }

    monkeypatch.setattr(language_cli.sg, "api", query_api)
    result = language_cli.main(
        [
            "--tranche",
            str(tranche_path),
            "--expected-tranche-sha256",
            tranche["tranche_sha256"],
            "--index-manifest",
            str(manifest_path),
            "--expected-index-manifest-sha256",
            manifest_sha256,
            "--observed-at",
            "2026-07-29T19:30:00Z",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    output = json.loads(output_path.read_text())
    assert output["repository_count"] == 3
    assert output["index_manifest_sha256"] == manifest_sha256
    assert output["inventory_sha256"] == repository_language_inventory_sha256(output)


def test_cli_rejects_self_consistent_index_manifest_remapping(tmp_path):
    tranche = _tranche()
    tranche["tranche_sha256"] = adoption_review_tranche_sha256(tranche)
    tranche_path = tmp_path / "tranche.json"
    manifest_path = tmp_path / "index-manifest.json"
    tranche_path.write_text(json.dumps(tranche))
    manifest = _manifest()
    manifest_path.write_text(json.dumps(manifest))
    frozen_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest["repositories"][0]["sourcegraph"]["mirror"][
        "name"
    ] = "github.com/sg-evals/unrelated-repository"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RepositoryLanguageError, match="index manifest checksum"):
        language_cli.main(
            [
                "--tranche",
                str(tranche_path),
                "--expected-tranche-sha256",
                tranche["tranche_sha256"],
                "--index-manifest",
                str(manifest_path),
                "--expected-index-manifest-sha256",
                frozen_sha256,
                "--observed-at",
                "2026-07-29T19:30:00Z",
                "--output",
                str(tmp_path / "languages.json"),
            ]
        )


def test_cli_rejects_tranche_hash_from_the_artifact_itself(tmp_path):
    tranche = _tranche()
    tranche_path = tmp_path / "tranche.json"
    manifest_path = tmp_path / "index-manifest.json"
    tranche_path.write_text(json.dumps(tranche))
    manifest_path.write_text(json.dumps({"repositories": []}))

    with pytest.raises(RepositoryLanguageError, match="expected tranche checksum"):
        language_cli.main(
            [
                "--tranche",
                str(tranche_path),
                "--expected-tranche-sha256",
                "c" * 64,
                "--index-manifest",
                str(manifest_path),
                "--expected-index-manifest-sha256",
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "--observed-at",
                "2026-07-29T19:30:00Z",
                "--output",
                str(tmp_path / "languages.json"),
            ]
        )
