"""Resumable, checksummed execution of the frozen Deep Search pilot."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from authorship.semantic_topology_protocol import canonical_sha256


class DeepSearchExecutionError(RuntimeError):
    """Raised when an API run cannot produce a terminal conversation."""


Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


def _http_transport(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")[:2000]
        raise DeepSearchExecutionError(
            f"Deep Search HTTP {error.code}: {body}"
        ) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise DeepSearchExecutionError(
            f"Deep Search transport error: {error}"
        ) from error


def _conversation(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("conversation", payload)


def _state(conversation: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    state = conversation.get("state") or {}
    for name in ("completed", "error", "canceled", "processing", "pending"):
        if name in state:
            detail = state.get(name)
            return name, detail if isinstance(detail, dict) else {}
    return "unknown", None


class DeepSearchClient:
    """Small client for Sourcegraph 7's versioned Deep Search API."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        transport: Transport = _http_transport,
        timeout: float = 330,
    ):
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("Sourcegraph endpoint must be HTTP(S)")
        if not token:
            raise ValueError("Sourcegraph token is required")
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self.transport = transport
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"token {self._token}",
            "User-Agent": "agent-code-authorship/1.0",
            "X-Requested-With": "agent-code-authorship 1.0",
        }

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.transport(
            f"{self.endpoint}/api/deepsearch.v1.Service/{method}",
            payload,
            self._headers,
            self.timeout,
        )

    def ask(
        self,
        question: str,
        *,
        poll_interval: float = 2,
        max_polls: int = 300,
    ) -> dict[str, Any]:
        created = self._call(
            "CreateConversation",
            {
                "parent": "users/~self",
                "conversation": {
                    "questions": [{"input": [{"question": {"text": question}}]}]
                },
            },
        )
        conversation = _conversation(created)
        identity = conversation.get("name")
        if not identity:
            raise DeepSearchExecutionError(
                "CreateConversation response lacks conversation name"
            )
        status, detail = _state(conversation)
        polls = 0
        while status in {"processing", "pending", "unknown"}:
            if polls >= max_polls:
                raise DeepSearchExecutionError(
                    f"Deep Search poll limit reached for {identity}"
                )
            if poll_interval:
                time.sleep(poll_interval)
            fetched = self._call(
                "GetConversation",
                {"name": identity},
            )
            conversation = _conversation(fetched)
            status, detail = _state(conversation)
            polls += 1
        error = detail if status in {"error", "canceled"} else None
        return {
            "terminal_status": status,
            "conversation_identity": identity,
            "raw_conversation": conversation,
            "error": error,
        }


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _extract_answer(conversation: dict[str, Any]) -> str:
    answers = []
    for value in _walk(conversation.get("questions", [])):
        if isinstance(value, dict) and isinstance(value.get("answer"), str):
            answers.append(value["answer"])
        if (
            isinstance(value, dict)
            and isinstance(value.get("markdown"), dict)
            and isinstance(value["markdown"].get("text"), str)
        ):
            answers.append(value["markdown"]["text"])
    return "\n\n".join(dict.fromkeys(answers))


def _extract_evidence(
    conversation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    searches: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    commits: list[str] = []
    for value in _walk(conversation):
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if name in {
            "keyword_search",
            "nls_search",
            "code_finder",
            "commit_search",
            "diff_search",
            "find_references",
            "go_to_definition",
        } and isinstance(value.get("input"), dict):
            searches.append({"tool": name, "input": value["input"]})
        path = value.get("path")
        repository = value.get("repository") or value.get("repo")
        if isinstance(path, str) and isinstance(repository, str):
            files.append(
                {
                    key: value[key]
                    for key in ("repository", "repo", "path", "url", "commit")
                    if key in value
                }
            )
        commit = value.get("commit")
        if isinstance(commit, str) and len(commit) == 40:
            commits.append(commit.lower())
    return (
        _unique_dicts(searches),
        _unique_dicts(files),
        sorted(set(commits)),
    )


def _unique_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        identity = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")
_SOURCEGRAPH_BLOB = re.compile(
    r"/r/(?P<repo>[^@]+)@(?P<commit>[0-9a-f]{40})/-/blob/" r"(?P<path>[^?#)]+)"
)
_QUERY_BLOCK = re.compile(
    r"(?:Deterministic query|Corpus-wide):\s*```(?:[a-z]+)?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def _extract_markdown_evidence(
    answer: str,
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    files = []
    commits = []
    for url in _MARKDOWN_LINK.findall(answer):
        match = _SOURCEGRAPH_BLOB.search(url)
        if not match:
            continue
        item = match.groupdict()
        files.append(
            {
                "repository": item["repo"],
                "path": item["path"],
                "commit": item["commit"],
                "url": url,
            }
        )
        commits.append(item["commit"])
    proposed_queries = [
        block.strip() for block in _QUERY_BLOCK.findall(answer) if block.strip()
    ]
    return (
        _unique_dicts(files),
        sorted(set(commits)),
        list(dict.fromkeys(proposed_queries)),
    )


def _artifact_path(output: Path, run_id: str) -> Path:
    return output / "runs" / f"{run_id.removeprefix('sha256:')}.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _make_artifact(
    run: dict[str, Any],
    plan: dict[str, Any],
    result: dict[str, Any],
    attempt_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    conversation = result.get("raw_conversation") or {}
    searches, files, commits = _extract_evidence(conversation)
    raw_answer = _extract_answer(conversation)
    markdown_files, markdown_commits, proposed_queries = _extract_markdown_evidence(
        raw_answer
    )
    files = _unique_dicts(files + markdown_files)
    commits = sorted(set(commits + markdown_commits))
    artifact: dict[str, Any] = {
        "$schema": "semantic-topology-deep-search-run.schema.json",
        "artifact_version": 1,
        "run_id": run["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "protocol_sha256": plan["protocol_sha256"],
        "canonical_repository_id": run["canonical_repository_id"],
        "prompt_family_id": run["prompt_family_id"],
        "prompt_sha256": run["prompt_sha256"],
        "prompt": run["prompt"],
        "terminal_status": result["terminal_status"],
        "conversation_identity": result.get("conversation_identity"),
        "search_context": {
            "repository": run["canonical_repository_id"],
            "context_revision_kind": run["context_revision_kind"],
        },
        "searches": searches,
        "search_trace_status": (
            "observed" if searches else "unavailable_not_exposed_by_api"
        ),
        "proposed_queries": proposed_queries,
        "cited_files": files,
        "citation_extraction_status": (
            "observed" if files else "none_extracted_from_api_response"
        ),
        "cited_commits": commits,
        "model_metadata": {
            "status": "unavailable",
            "reason": "not_exposed_by_api",
        },
        "raw_answer": raw_answer,
        "raw_conversation": conversation,
        "error": result.get("error"),
        "attempt_history": attempt_history or [],
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    return artifact


def validate_run_artifact(artifact: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    errors = []
    runs = {item["run_id"]: item for item in plan["runs"]}
    run = runs.get(artifact.get("run_id"))
    if not run:
        errors.append("run_id is not in frozen plan")
        return errors
    for key in (
        "canonical_repository_id",
        "prompt_family_id",
        "prompt_sha256",
        "prompt",
    ):
        if artifact.get(key) != run.get(key):
            errors.append(f"run artifact {key} mismatch")
    if artifact.get("plan_sha256") != plan.get("plan_sha256"):
        errors.append("run artifact plan_sha256 mismatch")
    if artifact.get("protocol_sha256") != plan.get("protocol_sha256"):
        errors.append("run artifact protocol_sha256 mismatch")
    if artifact.get("terminal_status") not in {
        "completed",
        "error",
        "canceled",
        "unavailable",
    }:
        errors.append("run artifact is not terminal")
    if canonical_sha256(artifact) != artifact.get("artifact_sha256"):
        errors.append("artifact_sha256 mismatch")
    return errors


def execute_plan(
    plan: dict[str, Any],
    output: Path,
    client: Any,
    *,
    limit: int | None = None,
    retry_terminal: set[str] | None = None,
    retry_error_codes: set[str] | None = None,
    rebuild_derived: bool = False,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    retry_terminal = retry_terminal or set()
    executed = 0
    artifacts = []
    for run_position, run in enumerate(plan["runs"]):
        attempt_history: list[dict[str, Any]] = []
        path = _artifact_path(output, run["run_id"])
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except json.JSONDecodeError:
                existing = {}
            can_rebuild = (
                existing.get("run_id") == run["run_id"]
                and existing.get("prompt_sha256") == run["prompt_sha256"]
                and isinstance(existing.get("raw_conversation"), dict)
                and existing.get("terminal_status")
                in {"completed", "error", "canceled", "unavailable"}
            )
            if rebuild_derived and can_rebuild:
                rebuilt = _make_artifact(
                    run,
                    plan,
                    {
                        "terminal_status": existing["terminal_status"],
                        "conversation_identity": existing.get("conversation_identity"),
                        "raw_conversation": existing.get("raw_conversation", {}),
                        "error": existing.get("error"),
                    },
                    attempt_history=existing.get("attempt_history", []),
                )
                _atomic_json(path, rebuilt)
                artifacts.append(rebuilt)
                continue
            retry_requested = existing.get("terminal_status") in retry_terminal and (
                retry_error_codes is None
                or (existing.get("error") or {}).get("code") in retry_error_codes
            )
            if not validate_run_artifact(existing, plan) and not retry_requested:
                artifacts.append(existing)
                continue
            if not validate_run_artifact(existing, plan) and retry_requested:
                attempt_history = [
                    *existing.get("attempt_history", []),
                    {
                        "terminal_status": existing["terminal_status"],
                        "conversation_identity": existing.get("conversation_identity"),
                        "error": existing.get("error"),
                        "artifact_sha256": existing["artifact_sha256"],
                    },
                ]
        if run_position % shard_count != shard_index:
            continue
        if limit is not None and executed >= limit:
            continue
        if run["status"] == "pending_context_revision":
            result = {
                "terminal_status": "unavailable",
                "conversation_identity": None,
                "raw_conversation": {},
                "error": {
                    "code": "context_revision_unresolved",
                    "message": "pinned pre-2023 revision must resolve before execution",
                },
            }
        else:
            try:
                result = client.ask(run["prompt"])
            except DeepSearchExecutionError as error:
                result = {
                    "terminal_status": "error",
                    "conversation_identity": None,
                    "raw_conversation": {},
                    "error": {
                        "code": "transport_error",
                        "message": str(error),
                    },
                }
        artifact = _make_artifact(run, plan, result, attempt_history=attempt_history)
        _atomic_json(path, artifact)
        artifacts.append(artifact)
        executed += 1

    artifacts_by_id = {item["run_id"]: item for item in artifacts}
    counts = Counter(item["terminal_status"] for item in artifacts_by_id.values())
    inventory: dict[str, Any] = {
        "$schema": "semantic-topology-deep-search-inventory.schema.json",
        "inventory_version": 1,
        "plan_sha256": plan["plan_sha256"],
        "protocol_sha256": plan["protocol_sha256"],
        "planned_run_count": len(plan["runs"]),
        "materialized_run_count": len(artifacts_by_id),
        "counts": dict(sorted(counts.items())),
        "runs": [
            {
                "run_id": run_id,
                "artifact_path": str(
                    _artifact_path(output, run_id).relative_to(output)
                ),
                "artifact_sha256": item["artifact_sha256"],
                "terminal_status": item["terminal_status"],
            }
            for run_id, item in sorted(artifacts_by_id.items())
        ],
    }
    inventory["inventory_sha256"] = canonical_sha256(inventory)
    _atomic_json(output / "inventory.v1.json", inventory)
    return inventory


def validate_inventory(
    inventory: dict[str, Any], plan: dict[str, Any], output: Path
) -> list[str]:
    errors = []
    if inventory.get("plan_sha256") != plan.get("plan_sha256"):
        errors.append("inventory plan_sha256 mismatch")
    if canonical_sha256(inventory) != inventory.get("inventory_sha256"):
        errors.append("inventory_sha256 mismatch")
    for entry in inventory.get("runs", []):
        path = output / entry["artifact_path"]
        if not path.exists():
            errors.append(f"missing run artifact: {entry['run_id']}")
            continue
        try:
            artifact = json.loads(path.read_text())
        except json.JSONDecodeError:
            errors.append(f"invalid run artifact JSON: {entry['run_id']}")
            continue
        errors.extend(validate_run_artifact(artifact, plan))
        if artifact.get("artifact_sha256") != entry.get("artifact_sha256"):
            errors.append(f"inventory run digest mismatch: {entry['run_id']}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/semantic-topology-pilot-plan.v1.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/semantic-topology-deep-search-v1"),
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry valid terminal error/canceled artifacts; preserve new result.",
    )
    parser.add_argument(
        "--retry-error-code",
        action="append",
        default=[],
        help="Retry only terminal errors with this code; may be repeated.",
    )
    parser.add_argument(
        "--rebuild-derived",
        action="store_true",
        help="Re-extract answers/citations from valid raw artifacts without API calls.",
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.validate_only:
        inventory = json.loads((args.output / "inventory.v1.json").read_text())
        errors = validate_inventory(inventory, plan, args.output)
        if errors:
            raise SystemExit("; ".join(errors))
        return
    endpoint = os.environ.get("SRC_ENDPOINT")
    token = os.environ.get("SRC_ACCESS_TOKEN")
    if not endpoint or not token:
        raise SystemExit("SRC_ENDPOINT and SRC_ACCESS_TOKEN are required")
    execute_plan(
        plan,
        args.output,
        DeepSearchClient(endpoint, token),
        limit=args.limit,
        retry_terminal=(
            {"error", "canceled"}
            if args.retry_errors
            else ({"error"} if args.retry_error_code else set())
        ),
        retry_error_codes=(
            set(args.retry_error_code) if args.retry_error_code else None
        ),
        rebuild_derived=args.rebuild_derived,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )


if __name__ == "__main__":
    main()
