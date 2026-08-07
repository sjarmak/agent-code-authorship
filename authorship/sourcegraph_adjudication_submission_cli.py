"""Submit the two frozen Sourcegraph adjudication reviewer batches."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_submission import (
    AdjudicationSubmissionError,
    submit_adjudication_batches,
)

DEFAULT_MANIFEST_A = Path("study/sourcegraph-adjudication-primary-a-batch.v3.json")
DEFAULT_MANIFEST_B = Path("study/sourcegraph-adjudication-primary-b-batch.v3.json")
DEFAULT_MANIFEST_A_SHA256 = (
    "c05d589601bdf98caddc42cb206836c47e828b80fe9b06bd94d2e9be502702b7"
)
DEFAULT_MANIFEST_B_SHA256 = (
    "7c8e3dc3d4e83615e3cfdc80dfc77f603a268d3d37ffc5a21e58f632fad8ef24"
)
DEFAULT_INPUT_ROOT = Path(
    "/mnt/agent-code-authorship/survival-study/"
    "sourcegraph-adjudication-v3/model-review/primary-a"
)
DEFAULT_JOURNAL = Path("study/sourcegraph-adjudication-batch-submission.v3.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-a", type=Path, default=DEFAULT_MANIFEST_A)
    parser.add_argument("--manifest-b", type=Path, default=DEFAULT_MANIFEST_B)
    parser.add_argument(
        "--expected-manifest-a-sha256",
        default=DEFAULT_MANIFEST_A_SHA256,
    )
    parser.add_argument(
        "--expected-manifest-b-sha256",
        default=DEFAULT_MANIFEST_B_SHA256,
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    return parser


def _load_manifest(path: Path, expected_sha256: str) -> Mapping[str, Any]:
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise AdjudicationSubmissionError(
            f"cannot read batch manifest {path}: {error}"
        ) from error
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise AdjudicationSubmissionError(
            f"batch manifest checksum does not match for {path}"
        )
    if not isinstance(document, Mapping):
        raise AdjudicationSubmissionError(f"batch manifest {path} must be an object")
    return document


def _client() -> Any:
    from openai import OpenAI

    return OpenAI(max_retries=0, timeout=1800.0)


def main(argv: Sequence[str] | None = None, *, client: Any = None) -> int:
    arguments = _parser().parse_args(argv)
    paths = (arguments.manifest_a, arguments.manifest_b)
    checksums = (
        arguments.expected_manifest_a_sha256,
        arguments.expected_manifest_b_sha256,
    )
    manifests = tuple(
        _load_manifest(path, checksum)
        for path, checksum in zip(paths, checksums, strict=True)
    )
    submit_adjudication_batches(
        client or _client(),
        manifests=manifests,
        manifest_paths=paths,
        expected_manifest_file_sha256s=checksums,
        input_root=arguments.input_root,
        journal_path=arguments.journal,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
