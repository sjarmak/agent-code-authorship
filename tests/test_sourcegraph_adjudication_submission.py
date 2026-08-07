import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from authorship.sourcegraph_adjudication_batch import batch_manifest_sha256
from authorship.sourcegraph_adjudication_submission import (
    AdjudicationSubmissionError,
    _batch_metadata,
    submit_adjudication_batches,
)
from authorship.sourcegraph_adjudication_submission_cli import _client, main


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manifest(reviewer_id: str, records: list[dict]) -> dict:
    document = {
        "batch_export_version": 3,
        "specification_sha256": "a" * 64,
        "queue_manifest_sha256": "b" * 64,
        "stage": "primary",
        "reviewer_id": reviewer_id,
        "reviewer_kind": "model",
        "reviewer_version": "sourcegraph-adjudication-openai-batch-v3",
        "provider": "openai",
        "model": "gpt-5.6",
        "reasoning_effort": "low",
        "prompt_sha256": "c" * 64,
        "endpoint": "/v1/responses",
        "request_count": sum(record["request_count"] for record in records),
        "max_requests_per_file": 49_000,
        "max_bytes_per_file": 190_000_000,
        "emitted_file_count": len(records),
        "files": records,
        "outcomes_consulted": False,
        "peer_reviews_consulted": False,
    }
    return {**document, "batch_manifest_sha256": batch_manifest_sha256(document)}


def _fixture(tmp_path: Path) -> tuple[Path, list[dict], dict, dict]:
    root = tmp_path / "inputs"
    request_root = root / "requests"
    request_root.mkdir(parents=True)
    records = []
    payloads = tuple(
        f'{{"custom_id":"request-{index}"}}\n'.encode() for index in range(5)
    )
    for index, payload in enumerate(payloads):
        path = request_root / f"{index:04d}.jsonl"
        path.write_bytes(payload)
        records.append(
            {
                "file": f"requests/{index:04d}.jsonl",
                "request_count": 1,
                "byte_count": len(payload),
                "first_custom_id": f"request-{index}",
                "last_custom_id": f"request-{index}",
                "sha256": _sha(payload),
            }
        )
    return (
        root,
        records,
        _manifest("reviewer-a", records),
        _manifest("reviewer-b", records),
    )


class _Page:
    def __init__(self, data: list[SimpleNamespace]):
        self.data = data

    def __iter__(self):
        return iter(self.data)


class _Files:
    def __init__(self):
        self.records: dict[str, SimpleNamespace] = {}
        self.create_calls = 0

    def list(self, **_kwargs):
        return _Page(list(self.records.values()))

    def create(self, *, file, purpose: str):
        filename, stream, _content_type = file
        payload = stream.read()
        self.create_calls += 1
        record = SimpleNamespace(
            id=f"file-{self.create_calls}",
            filename=filename,
            bytes=len(payload),
            purpose=purpose,
            created_at=100 + self.create_calls,
            status="processed",
            payload=payload,
        )
        self.records[record.id] = record
        return record

    def retrieve(self, file_id: str):
        return self.records[file_id]

    def content(self, file_id: str):
        payload = self.records[file_id].payload
        return SimpleNamespace(iter_bytes=lambda: iter((payload,)))


class _Batches:
    def __init__(self):
        self.records: dict[str, SimpleNamespace] = {}
        self.create_calls = 0

    def list(self, **_kwargs):
        return _Page(list(self.records.values()))

    def create(self, **kwargs):
        self.create_calls += 1
        record = SimpleNamespace(
            id=f"batch-{self.create_calls}",
            input_file_id=kwargs["input_file_id"],
            endpoint=kwargs["endpoint"],
            completion_window=kwargs["completion_window"],
            metadata=kwargs["metadata"],
            status="validating",
            created_at=200 + self.create_calls,
            output_file_id=None,
            error_file_id=None,
            request_counts=SimpleNamespace(total=0, completed=0, failed=0),
            errors=None,
        )
        self.records[record.id] = record
        return record

    def retrieve(self, batch_id: str):
        return self.records[batch_id]


class _Client:
    def __init__(self):
        self.files = _Files()
        self.batches = _Batches()


def test_submit_uploads_each_unique_file_once_and_creates_two_reviewer_jobs(
    tmp_path: Path,
):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    journal_path = tmp_path / "submission.json"
    client = _Client()

    result = submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal_path,
    )

    assert client.files.create_calls == 5
    assert client.batches.create_calls == 10
    assert len(result["input_files"]) == 5
    assert len(result["batches"]) == 10
    assert {batch["reviewer_id"] for batch in result["batches"]} == {
        "reviewer-a",
        "reviewer-b",
    }
    assert all(batch["endpoint"] == "/v1/responses" for batch in result["batches"])
    assert all(batch["completion_window"] == "24h" for batch in result["batches"])
    assert json.loads(journal_path.read_text()) == result

    resumed = submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal_path,
    )

    assert client.files.create_calls == 5
    assert client.batches.create_calls == 10
    assert resumed["input_files"] == result["input_files"]
    assert resumed["batches"] == result["batches"]


def test_submit_recovers_remote_objects_when_journal_write_was_interrupted(
    tmp_path: Path,
):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    initial_path = tmp_path / "initial.json"
    first = submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=initial_path,
    )

    recovered = submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=tmp_path / "recovered.json",
    )

    assert client.files.create_calls == 5
    assert client.batches.create_calls == 10
    assert {record["openai_file_id"] for record in recovered["input_files"]} == {
        record["openai_file_id"] for record in first["input_files"]
    }
    assert {record["batch_id"] for record in recovered["batches"]} == {
        record["batch_id"] for record in first["batches"]
    }


@pytest.mark.parametrize("drift", ["payload", "manifest", "reviewer", "file_count"])
def test_submit_rejects_local_contract_drift_before_api_calls(
    tmp_path: Path, drift: str
):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    if drift == "payload":
        (root / "requests/0000.jsonl").write_text("changed\n")
    elif drift == "manifest":
        manifest_b = {**manifest_b, "model": "other-model"}
        manifest_b["batch_manifest_sha256"] = batch_manifest_sha256(manifest_b)
    elif drift == "file_count":
        manifest_a = _manifest("reviewer-a", manifest_a["files"][:-1])
        manifest_b = _manifest("reviewer-b", manifest_b["files"][:-1])
    else:
        manifest_b = {**manifest_b, "reviewer_id": "reviewer-a"}
        manifest_b["batch_manifest_sha256"] = batch_manifest_sha256(manifest_b)
    client = _Client()

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_preflights_all_remote_drift_before_any_paid_mutation(tmp_path: Path):
    root, records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    drifted = records[-1]
    client.files.records["drifted"] = SimpleNamespace(
        id="drifted",
        filename=(f"sourcegraph-adjudication-v3-0004-{drifted['sha256'][:16]}.jsonl"),
        bytes=drifted["byte_count"] + 1,
        purpose="batch",
        created_at=1,
        status="processed",
        payload=(root / drifted["file"]).read_bytes(),
    )

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_preflights_remote_batch_drift_before_uploads(tmp_path: Path):
    root, records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    local = records[-1]
    file_id = "existing-file"
    client.files.records[file_id] = SimpleNamespace(
        id=file_id,
        filename=(f"sourcegraph-adjudication-v3-0004-{local['sha256'][:16]}.jsonl"),
        bytes=local["byte_count"],
        purpose="batch",
        created_at=1,
        status="processed",
        payload=(root / local["file"]).read_bytes(),
    )
    metadata = _batch_metadata(
        "reviewer-a",
        "0004",
        local["sha256"],
        manifest_a["batch_manifest_sha256"],
    )
    client.batches.records["drifted"] = SimpleNamespace(
        id="drifted",
        input_file_id="wrong-file",
        endpoint="/v1/responses",
        completion_window="24h",
        metadata=metadata,
        status="validating",
        created_at=2,
        output_file_id=None,
        error_file_id=None,
        request_counts=None,
        errors=None,
    )

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_matches_recovery_batch_by_submission_key_then_rejects_drift(
    tmp_path: Path,
):
    root, records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    for index, local in enumerate(records):
        client.files.records[f"existing-{index}"] = SimpleNamespace(
            id=f"existing-{index}",
            filename=(
                "sourcegraph-adjudication-v3-"
                f"{index:04d}-{local['sha256'][:16]}.jsonl"
            ),
            bytes=local["byte_count"],
            purpose="batch",
            created_at=index,
            status="processed",
            payload=(root / local["file"]).read_bytes(),
        )
    metadata = _batch_metadata(
        "reviewer-a",
        "0004",
        records[-1]["sha256"],
        manifest_a["batch_manifest_sha256"],
    )
    client.batches.records["drifted"] = SimpleNamespace(
        id="drifted",
        input_file_id="existing-4",
        endpoint="/v1/responses",
        completion_window="24h",
        metadata={**metadata, "reviewer": "wrong-reviewer"},
        status="validating",
        created_at=2,
        output_file_id=None,
        error_file_id=None,
        request_counts=None,
        errors=None,
    )

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_rejects_malformed_journal_before_uploads(tmp_path: Path):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    journal = tmp_path / "submission.json"
    first_client = _Client()
    submit_adjudication_batches(
        first_client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal,
    )
    document = json.loads(journal.read_text())
    document["input_files"] = []
    document["batches"] = [{}]
    journal.write_text(json.dumps(document))
    client = _Client()

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=journal,
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


@pytest.mark.parametrize(
    ("collection", "field", "value"),
    [
        ("input_files", "remote_filename", "tampered.jsonl"),
        ("batches", "endpoint", "/v1/chat/completions"),
    ],
)
def test_submit_rejects_immutable_journal_drift_before_paid_mutation(
    tmp_path: Path, collection: str, field: str, value: str
):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    journal = tmp_path / "submission.json"
    original = _Client()
    submit_adjudication_batches(
        original,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal,
    )
    document = json.loads(journal.read_text())
    document[collection][0][field] = value
    journal.write_text(json.dumps(document))
    original.files.create_calls = 0
    original.batches.create_calls = 0

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            original,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=journal,
        )

    assert original.files.create_calls == 0
    assert original.batches.create_calls == 0


def test_submit_hashes_remote_content_before_recovery(tmp_path: Path):
    root, records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    local = records[-1]
    wrong_payload = (
        (root / local["file"]).read_bytes().replace(b"request-4", b"request-X")
    )
    client.files.records["wrong-content"] = SimpleNamespace(
        id="wrong-content",
        filename=(f"sourcegraph-adjudication-v3-0004-{local['sha256'][:16]}.jsonl"),
        bytes=len(wrong_payload),
        purpose="batch",
        created_at=1,
        status="processed",
        payload=wrong_payload,
    )

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_snapshots_exact_bytes_before_upload(tmp_path: Path):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    target = root / "requests/0000.jsonl"
    original_list = client.files.list

    def mutate_then_list(**kwargs):
        payload = target.read_bytes().replace(b"request-0", b"request-X")
        target.write_bytes(payload)
        return original_list(**kwargs)

    client.files.list = mutate_then_list

    with pytest.raises(AdjudicationSubmissionError):
        submit_adjudication_batches(
            client,
            manifests=(manifest_a, manifest_b),
            manifest_paths=(Path("a.json"), Path("b.json")),
            expected_manifest_file_sha256s=("d" * 64, "e" * 64),
            input_root=root,
            journal_path=tmp_path / "submission.json",
        )

    assert client.files.create_calls == 0
    assert client.batches.create_calls == 0


def test_submit_serializes_concurrent_invocations_with_one_journal(tmp_path: Path):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    entered = threading.Event()
    release = threading.Event()
    original_list = client.files.list
    list_calls = 0

    def blocking_list(**kwargs):
        nonlocal list_calls
        list_calls += 1
        if list_calls == 1:
            entered.set()
            assert release.wait(timeout=5)
        return original_list(**kwargs)

    client.files.list = blocking_list
    journal = tmp_path / "submission.json"
    errors: list[BaseException] = []

    def submit():
        try:
            submit_adjudication_batches(
                client,
                manifests=(manifest_a, manifest_b),
                manifest_paths=(Path("a.json"), Path("b.json")),
                expected_manifest_file_sha256s=("d" * 64, "e" * 64),
                input_root=root,
                journal_path=journal,
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=submit)
    second = threading.Thread(target=submit)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert not first.is_alive()
    assert not second.is_alive()
    assert client.files.create_calls == 5
    assert client.batches.create_calls == 10


def test_submit_serializes_openai_batch_error_models(tmp_path: Path):
    from openai.types.batch import BatchError, Errors

    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    client = _Client()
    journal = tmp_path / "submission.json"
    submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal,
    )
    first = next(iter(client.batches.records.values()))
    first.errors = Errors(
        data=[BatchError(code="invalid", message="bad input", line=1, param=None)]
    )
    first.status = "failed"

    result = submit_adjudication_batches(
        client,
        manifests=(manifest_a, manifest_b),
        manifest_paths=(Path("a.json"), Path("b.json")),
        expected_manifest_file_sha256s=("d" * 64, "e" * 64),
        input_root=root,
        journal_path=journal,
    )

    assert result["batches"][0]["errors"]["data"][0]["code"] == "invalid"
    assert json.loads(journal.read_text())["batches"][0]["status"] == "failed"


def test_submission_cli_verifies_manifest_files_and_writes_journal(tmp_path: Path):
    root, _records, manifest_a, manifest_b = _fixture(tmp_path)
    paths = (tmp_path / "a.json", tmp_path / "b.json")
    for path, manifest in zip(paths, (manifest_a, manifest_b), strict=True):
        path.write_text(json.dumps(manifest, sort_keys=True))
    expected = tuple(_sha(path.read_bytes()) for path in paths)
    journal = tmp_path / "submission.json"
    client = _Client()

    assert (
        main(
            [
                "--manifest-a",
                str(paths[0]),
                "--manifest-b",
                str(paths[1]),
                "--expected-manifest-a-sha256",
                expected[0],
                "--expected-manifest-b-sha256",
                expected[1],
                "--input-root",
                str(root),
                "--journal",
                str(journal),
            ],
            client=client,
        )
        == 0
    )
    assert len(json.loads(journal.read_text())["batches"]) == 10

    with pytest.raises(AdjudicationSubmissionError):
        main(
            [
                "--manifest-a",
                str(paths[0]),
                "--manifest-b",
                str(paths[1]),
                "--expected-manifest-a-sha256",
                "0" * 64,
                "--expected-manifest-b-sha256",
                expected[1],
                "--input-root",
                str(root),
                "--journal",
                str(tmp_path / "bad.json"),
            ],
            client=_Client(),
        )


def test_openai_client_disables_automatic_post_retries(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")

    assert _client().max_retries == 0
