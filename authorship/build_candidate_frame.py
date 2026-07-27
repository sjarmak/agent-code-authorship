"""Build the frozen candidate frame from saved outcome-blind GitHub metadata."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from authorship.candidates import selection_key


QUERIES = {
    "Python": (
        "language:Python fork:false archived:false created:<2024-01-01 "
        "pushed:>=2025-01-01 size:500..50000 stars:20..2000"
    ),
    "Go": (
        "language:Go fork:false archived:false created:<2024-01-01 "
        "pushed:>=2025-01-01 size:500..50000 stars:20..2000"
    ),
}
AGENT_GO_IDS = {
    "gastownhall/gascity-packs",
    "gastownhall/gastown",
    "gastownhall/wasteland",
    "gastownhall/tmux-adapter",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(item: dict, language: str, proposed_label: str, source: str) -> dict:
    repo_id = item.get("full_name") or item["nameWithOwner"]
    metadata = {
        "created_at": item.get("created_at"),
        "pushed_at": item.get("pushed_at") or item.get("pushedAt"),
        "stars": item.get("stargazers_count"),
        "disk_kib": item.get("size") or item.get("diskUsage"),
        "has_issues": item.get("has_issues", True),
        "owner_type": (item.get("owner") or {}).get("type"),
        "is_fork": item.get("fork", item.get("isFork", False)),
        "is_archived": item.get("archived", item.get("isArchived", False)),
    }
    candidate = {
        "id": repo_id,
        "url": item.get("html_url") or item["url"],
        "language": language,
        "proposed_label": proposed_label,
        "source": source,
        "content_group": repo_id,
        "metadata": metadata,
        "solicitation_status": "unsolicited",
    }
    candidate["selection_key"] = selection_key(candidate)
    return candidate


def build(
    python_search: dict,
    go_search: dict,
    gastown_repositories: list[dict],
    target_manifest: dict,
    reference_manifest: dict,
    *,
    root: Path,
) -> dict:
    excluded = {
        entry["id"].lower()
        for manifest in (target_manifest, reference_manifest)
        for entry in manifest["repositories"]
    }
    candidates = []
    for language, response in (("Python", python_search), ("Go", go_search)):
        for item in response["items"]:
            if (
                item["full_name"].lower() not in excluded
                and item["has_issues"]
                and not item["fork"]
                and not item["archived"]
            ):
                candidates.append(
                    _candidate(item, language, "human", "github_search")
                )
    for item in gastown_repositories:
        if item["nameWithOwner"].lower() in {
            repo_id.lower() for repo_id in AGENT_GO_IDS
        }:
            candidates.append(
                _candidate(item, "Go", "agent", "maintainer_organization_frame")
            )
    candidates.sort(key=lambda candidate: candidate["selection_key"])
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "$schema": "reference-candidates.schema.json",
        "frame_version": 2,
        "status": "frozen_before_solicitation",
        "retrieved_at": now,
        "amendment_sha256": _sha256(
            root / "study" / "protocol-amendment.v2.json"
        ),
        "target_manifest_sha256": _sha256(root / "study" / "targets.v1.json"),
        "reference_manifest_sha256": _sha256(
            root / "study" / "repositories.v1.json"
        ),
        "selection": {
            "queries": QUERIES,
            "github_sort": "stars_descending",
            "github_page_size": 100,
            "final_order": "sha256(proposed_label + NUL + language + NUL + lowercase_repository_id)",
            "classifier_outputs_consulted": False,
            "source_features_consulted": False,
        },
        "candidates": candidates,
    }


def _write(path: Path, document: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    document = build(
        json.loads(Path("/tmp/human-python-frame.json").read_text()),
        json.loads(Path("/tmp/human-go-frame.json").read_text()),
        json.loads(Path("/tmp/gastownhall-repos.json").read_text()),
        json.loads((root / "study" / "targets.v1.json").read_text()),
        json.loads((root / "study" / "repositories.v1.json").read_text()),
        root=root,
    )
    destination = root / "study" / "reference-candidates.v2.json"
    _write(destination, document)
    print(destination)


if __name__ == "__main__":
    main()
