"""Collect outcome-blind GitHub facts needed for default-branch eligibility."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any


CUTOFF = "2026-07-24T23:59:59Z"


def graphql_query(owner: str, name: str, numbers: list[int]) -> str:
    fields = " ".join(
        f"p{index}:pullRequest(number:{number})"
        "{number baseRefName merged mergedAt mergeCommit{oid}}"
        for index, number in enumerate(numbers)
    )
    return (
        "query{repository(owner:"
        + json.dumps(owner)
        + ",name:"
        + json.dumps(name)
        + "){defaultBranchRef{name} "
        + fields
        + "}}"
    )


def classify_repository(
    repository_id: str, numbers: list[int], batches: list[dict[str, Any]]
) -> dict[str, Any]:
    default_branch = next(
        (
            batch["defaultBranchRef"]["name"]
            for batch in batches
            if batch.get("defaultBranchRef")
        ),
        None,
    )
    eligible = []
    eligible_prs = []
    reasons: dict[str, list[int]] = {
        "missing": [],
        "not_merged": [],
        "after_cutoff": [],
        "base_branch_mismatch": [],
    }
    for batch in batches:
        for key, pr in batch.items():
            if not key.startswith("p"):
                continue
            if not pr:
                continue
            number = int(pr["number"])
            if not pr.get("merged"):
                reasons["not_merged"].append(number)
            elif not pr.get("mergedAt") or pr["mergedAt"] > CUTOFF:
                reasons["after_cutoff"].append(number)
            elif pr.get("baseRefName") != default_branch:
                reasons["base_branch_mismatch"].append(number)
            else:
                eligible.append(number)
                eligible_prs.append(
                    {
                        "number": number,
                        "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
                        "merged_at": pr["mergedAt"],
                        "base_ref": pr["baseRefName"],
                    }
                )
    observed = {
        int(pr["number"])
        for batch in batches
        for key, pr in batch.items()
        if key.startswith("p") and pr
    }
    reasons["missing"] = sorted(set(numbers) - observed)
    return {
        "repository_id": repository_id,
        "default_branch": default_branch,
        "eligible_pr_numbers": sorted(set(eligible)),
        "eligible_prs": sorted(eligible_prs, key=lambda item: item["number"]),
        "ineligible_prs": {key: sorted(set(value)) for key, value in reasons.items()},
    }


def apply_reachability(
    frame: dict[str, Any], facts: dict[str, Any]
) -> dict[str, Any]:
    by_repository = {
        item["repository_id"]: item for item in facts["repositories"]
    }
    candidates = []
    for original in frame["candidates"]:
        candidate = dict(original)
        fact = by_repository.get(candidate["repository_id"])
        if not fact or fact.get("retrieval_errors"):
            continue
        eligible = set(fact["eligible_pr_numbers"])
        eligible_facts = {
            item["number"]: item for item in fact.get("eligible_prs", [])
        }
        pull_requests = [
            pr
            for pr in candidate["pull_requests"]
            if pr["number"] in eligible
            and eligible_facts.get(pr["number"], {}).get("merge_commit")
        ]
        for pr in pull_requests:
            pr.update(eligible_facts.get(pr["number"], {}))
        attributable_lines = sum(
            pr["attributable_added_lines"] for pr in pull_requests
        )
        if attributable_lines < 200:
            continue
        candidate["pull_requests"] = pull_requests
        candidate["pr_ids"] = sorted(pr["id"] for pr in pull_requests)
        candidate["pr_numbers"] = sorted(pr["number"] for pr in pull_requests)
        candidate["pr_urls"] = sorted(pr["url"] for pr in pull_requests)
        candidate["commit_shas"] = sorted(
            {sha for pr in pull_requests for sha in pr["commit_shas"]}
        )
        candidate["attributable_added_lines"] = attributable_lines
        candidate["first_merged_at"] = min(pr["merged_at"] for pr in pull_requests)
        candidate["last_merged_at"] = max(pr["merged_at"] for pr in pull_requests)
        candidate["default_branch"] = fact["default_branch"]
        candidate["default_branch_status"] = "github_verified"
        candidates.append(candidate)
    result = dict(frame)
    result["status"] = "github_default_branch_verified"
    result["candidates"] = candidates
    return result


def fetch_batch(owner: str, name: str, numbers: list[int]) -> dict[str, Any]:
    query = graphql_query(owner, name, numbers)
    for attempt in range(4):
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            return (payload.get("data") or {}).get("repository") or {}
        if attempt == 3:
            raise RuntimeError(
                f"GitHub GraphQL failed for {owner}/{name}: {result.stderr[:300]}"
            )
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def collect(frame: dict[str, Any], batch_size: int = 50) -> dict[str, Any]:
    repositories = []
    for index, candidate in enumerate(frame["candidates"], start=1):
        repository_id = candidate["repository_id"]
        owner, name = repository_id.split("/", 1)
        numbers = candidate["pr_numbers"]
        batches = [
            _safe_fetch_batch(owner, name, numbers[offset : offset + batch_size])
            for offset in range(0, len(numbers), batch_size)
        ]
        result = classify_repository(repository_id, numbers, batches)
        errors = [batch["_error"] for batch in batches if "_error" in batch]
        if errors:
            result["retrieval_errors"] = errors
        repositories.append(result)
        print(f"{index}/{len(frame['candidates'])} {repository_id}", flush=True)
    return {
        "facts_version": 1,
        "source": "GitHub GraphQL",
        "cutoff": CUTOFF,
        "outcomes_consulted": False,
        "repositories": repositories,
    }


def _safe_fetch_batch(owner: str, name: str, numbers: list[int]) -> dict[str, Any]:
    try:
        return fetch_batch(owner, name, numbers)
    except RuntimeError as error:
        return {"_error": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/raw/github/"
            "survival-default-branch-facts.v1.json"
        ),
    )
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    document = collect(json.loads(arguments.frame.read_text()))
    arguments.output.write_text(json.dumps(document, indent=2) + "\n")
    print(arguments.output)


if __name__ == "__main__":
    main()
