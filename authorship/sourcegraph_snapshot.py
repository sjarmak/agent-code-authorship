"""Audit direct Sourcegraph snapshot availability for the frozen frame."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from authorship.sg import api, check_auth


QUERY = (
    'query($repo:String!){repository(name:$repo){name mirrorInfo{cloned} '
    'commit(rev:"HEAD"){oid}}}'
)


def collect(frame: dict) -> dict:
    username = check_auth()
    records = []
    for index, candidate in enumerate(frame["candidates"], start=1):
        repository_id = candidate["repository_id"]
        sourcegraph_name = f"github.com/{repository_id}"
        repository = api(QUERY, repo=sourcegraph_name).get("repository")
        commit = (repository or {}).get("commit") or {}
        records.append(
            {
                "repository_id": repository_id,
                "sourcegraph_name": sourcegraph_name,
                "directly_indexed": bool(repository),
                "cloned": bool(((repository or {}).get("mirrorInfo") or {}).get("cloned")),
                "head_oid": commit.get("oid"),
            }
        )
        print(f"{index}/{len(frame['candidates'])} {repository_id}", flush=True)
    return {
        "audit_version": 1,
        "sourcegraph_user": username,
        "endpoint_role": "discovery_and_snapshot_verification",
        "outcomes_consulted": False,
        "repositories": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/raw/sourcegraph/"
            "survival-snapshot-audit.v1.json"
        ),
    )
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    document = collect(json.loads(arguments.frame.read_text()))
    arguments.output.write_text(json.dumps(document, indent=2) + "\n")
    print(arguments.output)


if __name__ == "__main__":
    main()
