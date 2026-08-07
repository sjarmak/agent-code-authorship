import hashlib
import json
from pathlib import Path

import pytest

from authorship import sourcegraph_discovery_execution_cli as execution_cli


@pytest.mark.parametrize(("status", "expected"), [("complete", 0), ("incomplete", 2)])
def test_cli_returns_status_and_hashes_input_artifacts(
    tmp_path: Path, monkeypatch, capsys, status: str, expected: int
):
    paths = []
    for name in ("protocol", "specification", "manifest", "audit"):
        path = tmp_path / f"{name}.json"
        path.write_text("{}")
        paths.append(path)
    paths[1].write_text(
        json.dumps({"protocol_sha256": hashlib.sha256(b"{}").hexdigest()})
    )
    observed = {}
    monkeypatch.setattr(execution_cli, "check_auth", lambda: "user")

    def execute(*_args, **kwargs):
        observed.update(kwargs)
        return {"status": status}

    monkeypatch.setattr(execution_cli, "execute_discovery", execute)

    result = execution_cli.main(
        [
            "--specification",
            str(paths[1]),
            "--protocol",
            str(paths[0]),
            "--index-manifest",
            str(paths[2]),
            "--index-audit",
            str(paths[3]),
            "--output-directory",
            str(tmp_path / "output"),
            "--retry-execution-errors-only",
        ]
    )

    assert result == expected
    assert observed["index_manifest_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert observed["index_audit_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert observed["retry_execution_errors_only"] is True
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize("content", ["{", "[]"])
def test_cli_rejects_invalid_input_documents(tmp_path: Path, monkeypatch, content: str):
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    monkeypatch.setattr(execution_cli, "check_auth", lambda: "user")

    with pytest.raises(SystemExit, match="must contain|cannot load"):
        execution_cli.main(["--specification", str(bad)])


def test_cli_rejects_protocol_hash_mismatch(tmp_path: Path, monkeypatch):
    protocol = tmp_path / "protocol.json"
    specification = tmp_path / "specification.json"
    protocol.write_text("{}")
    specification.write_text(json.dumps({"protocol_sha256": "0" * 64}))
    monkeypatch.setattr(execution_cli, "check_auth", lambda: "user")

    with pytest.raises(SystemExit, match="protocol SHA-256"):
        execution_cli.main(
            [
                "--protocol",
                str(protocol),
                "--specification",
                str(specification),
            ]
        )


def test_cli_passes_exact_repository_exclusion_source_bytes(
    tmp_path: Path, monkeypatch
):
    protocol = tmp_path / "protocol.json"
    specification = tmp_path / "specification.json"
    manifest = tmp_path / "manifest.json"
    audit = tmp_path / "audit.json"
    source = tmp_path / "action-plan.json"
    protocol.write_text("{}")
    manifest.write_text("{}")
    audit.write_text("{}")
    source.write_bytes(b'{"plan":"bytes"}')
    specification.write_text(
        json.dumps(
            {
                "protocol_sha256": hashlib.sha256(b"{}").hexdigest(),
                "execution": {
                    "repository_exclusions": {
                        "repositories": [{"canonical_repository_id": "org/repo"}]
                    }
                },
            }
        )
    )
    observed = {}
    monkeypatch.setattr(execution_cli, "check_auth", lambda: "user")

    def execute(*_args, **kwargs):
        observed.update(kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(execution_cli, "execute_discovery", execute)

    result = execution_cli.main(
        [
            "--specification",
            str(specification),
            "--protocol",
            str(protocol),
            "--index-manifest",
            str(manifest),
            "--index-audit",
            str(audit),
            "--repository-exclusion-source",
            str(source),
            "--output-directory",
            str(tmp_path / "output"),
        ]
    )

    assert result == 0
    assert observed["repository_exclusion_source"] == b'{"plan":"bytes"}'
