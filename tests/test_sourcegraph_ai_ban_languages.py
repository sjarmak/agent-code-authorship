import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_ai_ban_languages_cli as language_cli
from authorship.sourcegraph_ai_ban_languages import (
    AiBanLanguageError,
    ai_ban_language_inventory_sha256,
    build_ai_ban_language_inventory,
)

ROOT = Path(__file__).parents[1]


def _target() -> dict:
    return json.loads(
        (ROOT / "study/sourcegraph-ai-ban-target-manifest.v3.json").read_text()
    )


def _query(language: str = "Go"):
    def query_api(_query: str, **variables: str) -> dict:
        return {
            f"repository{index}": {"name": name, "language": language}
            for index, name in enumerate(variables.values())
        }

    return query_api


def test_inventory_queries_all_preregistered_controls_from_sg_evals():
    target = _target()

    inventory = build_ai_ban_language_inventory(
        target,
        _query(),
        index_manifest_file_sha256="a" * 64,
        observed_at="2026-07-29T21:02:00Z",
    )

    assert inventory["repository_count"] == 17
    assert inventory["language_counts"] == {"Go": 17}
    assert {row["canonical_repository_id"] for row in inventory["repositories"]} == {
        row["canonical_repository_id"] for row in target["repositories"]
    }
    assert all(
        row["language_sourcegraph_name"].startswith("github.com/sg-evals/")
        for row in inventory["repositories"]
    )
    assert inventory["sourcegraph_capability"] == "Repository.language"
    assert inventory["scip_required"] is False
    assert inventory["outcomes_consulted"] is False
    assert inventory["inventory_sha256"] == ai_ban_language_inventory_sha256(inventory)
    jsonschema.validate(
        inventory,
        json.loads(
            (
                ROOT / "study/sourcegraph-ai-ban-language-inventory.schema.json"
            ).read_text()
        ),
    )


def test_inventory_rejects_target_drift_and_non_sg_evals_names():
    target = _target()
    target["repositories"][0]["sourcegraph_name"] = "github.com/direct/repository"

    with pytest.raises(AiBanLanguageError, match="checksum"):
        build_ai_ban_language_inventory(
            target,
            _query(),
            index_manifest_file_sha256="a" * 64,
            observed_at="2026-07-29T21:02:00Z",
        )

    target = copy.deepcopy(_target())
    target["repositories"][0]["sourcegraph_name"] = "github.com/direct/repository"
    from authorship.sourcegraph_ai_ban_review import ai_ban_target_manifest_sha256

    target["target_manifest_sha256"] = ai_ban_target_manifest_sha256(target)
    with pytest.raises(AiBanLanguageError, match="sg-evals"):
        build_ai_ban_language_inventory(
            target,
            _query(),
            index_manifest_file_sha256="a" * 64,
            observed_at="2026-07-29T21:02:00Z",
        )


def test_cli_pins_target_and_index_manifest_files(monkeypatch, tmp_path):
    target = _target()
    target_path = tmp_path / "target.json"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "languages.json"
    target_path.write_text(json.dumps(target))
    manifest_path.write_bytes(
        (ROOT / "study/sourcegraph-index-manifest.v3.json").read_bytes()
    )
    target_file_sha = hashlib.sha256(target_path.read_bytes()).hexdigest()
    manifest_file_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    monkeypatch.setattr(language_cli.sg, "api", _query("Python"))

    assert (
        language_cli.main(
            [
                "--target",
                str(target_path),
                "--expected-target-file-sha256",
                target_file_sha,
                "--expected-target-manifest-sha256",
                target["target_manifest_sha256"],
                "--index-manifest",
                str(manifest_path),
                "--expected-index-manifest-file-sha256",
                manifest_file_sha,
                "--observed-at",
                "2026-07-29T21:02:00Z",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    assert json.loads(output_path.read_text())["language_counts"] == {"Python": 17}

    with pytest.raises(AiBanLanguageError, match="independent pin"):
        language_cli.main(
            [
                "--target",
                str(target_path),
                "--expected-target-file-sha256",
                "0" * 64,
                "--expected-target-manifest-sha256",
                target["target_manifest_sha256"],
                "--index-manifest",
                str(manifest_path),
                "--expected-index-manifest-file-sha256",
                manifest_file_sha,
                "--observed-at",
                "2026-07-29T21:02:00Z",
                "--output",
                str(output_path),
            ]
        )
