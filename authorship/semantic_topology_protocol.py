"""Freeze and validate the Sourcegraph semantic-topology pilot.

Deep Search is deliberately kept on the evidence side of the trust boundary:
it may discover a claim, but only deterministic Sourcegraph, pinned Git, or a
blinded human decision may populate an analytic field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


class SemanticTopologyProtocolError(ValueError):
    """Raised when a pilot artifact violates a locked research decision."""


PILOT_STRATA = {
    "agent_native": 5,
    "attributable_agent": 5,
    "policy_control": 5,
    "pre_2023_context": 5,
}
PROMPT_IDS = [
    "adoption_configuration",
    "delegated_task_intent",
    "review_validation",
    "semantic_controls",
    "change_topology",
]
ADMISSIBLE_ROUTES = {
    "deterministic_sourcegraph",
    "pinned_git",
    "blinded_human_adjudication",
}
ANALYTIC_FIELDS = [
    "task_taxonomy",
    "blast_radius",
    "ownership",
    "tests_and_docs",
    "semantic_hard_negatives",
    "diffusion",
    "subsequent_changes",
    "human_assimilation",
    "observability",
    "uncertainty",
]


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(document: dict[str, Any]) -> str:
    """Hash canonical JSON without its self-referential top-level digest."""
    excluded = {
        "protocol_sha256",
        "plan_sha256",
        "artifact_sha256",
        "inventory_sha256",
    }
    contextual_self_digests = {
        "packet_version": "packet_sha256",
        "result_version": "result_sha256",
        "verification_version": "verification_sha256",
        "finalization_version": "finalization_sha256",
        "audit_version": "audit_sha256",
        "summary_version": "summary_sha256",
    }
    excluded.update(
        digest
        for marker, digest in contextual_self_digests.items()
        if marker in document
    )
    content = {key: value for key, value in document.items() if key not in excluded}
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _selected_index(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["canonical_repository_id"].lower(): item
        for item in index["repositories"]
        if item.get("sourcegraph", {}).get("selected_name")
        and item.get("sourcegraph", {}).get("transport_status")
        in {"ready_direct", "ready_mirror"}
    }


def _repo_entry(
    *,
    repository_id: str,
    stratum: str,
    index_entry: dict[str, Any],
    source_artifact: str,
    source_sha256: str,
    selection_basis: str,
    event: dict[str, Any] | None = None,
    context_commit: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "canonical_repository_id": repository_id,
        "canonical_source_url": index_entry["canonical_source_url"],
        "sourcegraph_name": index_entry["sourcegraph"]["selected_name"],
        "cutoff_commit": index_entry["cutoff_commit"],
        "stratum": stratum,
        "selection_basis": selection_basis,
        "source_artifact": source_artifact,
        "source_sha256": source_sha256,
        "context_revision": {
            "kind": (
                "last_default_branch_commit_before_2023"
                if stratum == "pre_2023_context"
                else "cutoff"
            ),
            "status": (
                "resolved"
                if stratum != "pre_2023_context" or context_commit
                else "pending_pinned_git_resolution"
            ),
            "commit": (
                context_commit
                if stratum == "pre_2023_context"
                else index_entry["cutoff_commit"]
            ),
        },
    }
    if event:
        result["introduction_event"] = event
    return result


def build_pilot_plan(protocol: dict[str, Any], root: Path) -> dict[str, Any]:
    """Build the outcome-blind 20-repository and 100-run plan."""
    inputs = protocol["input_artifacts"]
    repositories_path = root / inputs["repository_evidence"]
    survival_path = root / inputs["attributable_events"]
    index_path = root / inputs["sourcegraph_index"]
    contextual_path = root / inputs["contextual_frame"]
    contextual_revisions_path = root / inputs["contextual_revisions"]
    repositories_doc = _read(repositories_path)
    survival_doc = _read(survival_path)
    index_doc = _read(index_path)
    contextual_doc = _read(contextual_path)
    contextual_revisions_doc = _read(contextual_revisions_path)
    indexed = _selected_index(index_doc)
    repository_sha = _file_sha256(repositories_path)
    survival_sha = _file_sha256(survival_path)
    contextual_revisions_sha = _file_sha256(contextual_revisions_path)

    selected: list[dict[str, Any]] = []
    by_id = {item["id"].lower(): item for item in repositories_doc["repositories"]}

    native_candidates = []
    for preferred_id in protocol["selection"]["agent_native_priority"]:
        evidence = by_id.get(preferred_id.lower())
        index_entry = indexed.get(preferred_id.lower())
        if (
            evidence
            and index_entry
            and evidence.get("label") == "agent"
            and evidence.get("evidence", {}).get("tier") == 1
            and evidence.get("evidence", {}).get("scope") == "entire_repository_history"
        ):
            native_candidates.append((preferred_id.lower(), index_entry))
    if len(native_candidates) < 5:
        raise SemanticTopologyProtocolError(
            "fewer than five indexed Tier-1 entire-history agent repositories"
        )
    for repository_id, index_entry in native_candidates[:5]:
        selected.append(
            _repo_entry(
                repository_id=repository_id,
                stratum="agent_native",
                index_entry=index_entry,
                source_artifact=inputs["repository_evidence"],
                source_sha256=repository_sha,
                selection_basis="frozen_priority_among_tier1_entire_history_attestations",
            )
        )

    attributable = []
    for candidate in survival_doc["candidates"]:
        repository_id = candidate["repository_id"].lower()
        index_entry = indexed.get(repository_id)
        if not index_entry:
            continue
        event = {
            "agent_family": candidate["agent_family"],
            "provenance_tier": candidate["provenance_tier"],
            "commit_shas": candidate["commit_shas"],
            "first_merged_at": candidate["first_merged_at"],
            "language": candidate["language"],
        }
        attributable.append(
            (
                candidate["selection_key"],
                repository_id,
                index_entry,
                event,
            )
        )
    attributable.sort()
    used = {item["canonical_repository_id"] for item in selected}
    for _, repository_id, index_entry, event in attributable:
        if repository_id in used:
            continue
        selected.append(
            _repo_entry(
                repository_id=repository_id,
                stratum="attributable_agent",
                index_entry=index_entry,
                source_artifact=inputs["attributable_events"],
                source_sha256=survival_sha,
                selection_basis="lowest_frozen_selection_key",
                event=event,
            )
        )
        used.add(repository_id)
        if sum(item["stratum"] == "attributable_agent" for item in selected) == 5:
            break

    policy_candidates = sorted(
        (
            item
            for item in repositories_doc["repositories"]
            if item.get("label") == "human"
            and item.get("evidence", {}).get("tier") == 1
            and item.get("evidence", {}).get("kind") == "repository_policy"
            and item["id"].lower() in indexed
            and item["id"].lower() not in used
        ),
        key=lambda item: item["id"].lower(),
    )
    for evidence in policy_candidates[:5]:
        repository_id = evidence["id"].lower()
        selected.append(
            _repo_entry(
                repository_id=repository_id,
                stratum="policy_control",
                index_entry=indexed[repository_id],
                source_artifact=inputs["repository_evidence"],
                source_sha256=repository_sha,
                selection_basis="lexicographic_tier1_repository_policy",
                event={
                    "policy_effective_from": evidence["evidence"]["effective_from"],
                    "policy_introduced_commit": evidence["evidence"][
                        "introduced_commit"
                    ],
                },
            )
        )
        used.add(repository_id)

    contextual_ids = {
        item["id"].lower()
        for item in contextual_doc["repositories"]
        if item.get("label") == "unlabeled"
    }
    for revision in contextual_revisions_doc["repositories"]:
        repository_id = revision["canonical_repository_id"].lower()
        if (
            repository_id not in contextual_ids
            or repository_id not in indexed
            or repository_id in used
        ):
            continue
        selected.append(
            _repo_entry(
                repository_id=repository_id,
                stratum="pre_2023_context",
                index_entry=indexed[repository_id],
                source_artifact=inputs["contextual_revisions"],
                source_sha256=contextual_revisions_sha,
                selection_basis=(
                    "frozen_lexicographic_unlabeled_target_with_pre2023_revision"
                ),
                context_commit=revision["commit"],
            )
        )
        used.add(repository_id)

    counts = Counter(item["stratum"] for item in selected)
    if counts != Counter(PILOT_STRATA):
        raise SemanticTopologyProtocolError(f"pilot strata incomplete: {dict(counts)}")

    runs = []
    prompts = {item["id"]: item for item in protocol["prompt_families"]}
    for repository in selected:
        for prompt_id in PROMPT_IDS:
            prompt = prompts[prompt_id]["template"].format(
                canonical_repository_id=repository["canonical_repository_id"],
                sourcegraph_name=repository["sourcegraph_name"],
                cutoff_commit=repository["cutoff_commit"],
                context_revision=repository["context_revision"]["commit"]
                or "PENDING_PRE_2023_REVISION",
                stratum=repository["stratum"],
            )
            identity = {
                "canonical_repository_id": repository["canonical_repository_id"],
                "prompt_family_id": prompt_id,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "context_revision_kind": repository["context_revision"]["kind"],
            }
            run_id = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            runs.append(
                {
                    "run_id": run_id,
                    **identity,
                    "prompt": prompt,
                    "status": (
                        "pending_context_revision"
                        if repository["context_revision"]["commit"] is None
                        else "frozen"
                    ),
                }
            )

    result: dict[str, Any] = {
        "$schema": "semantic-topology-pilot-plan.schema.json",
        "plan_version": 1,
        "status": "preregistered_pre_execution",
        "cutoff": protocol["cutoff"],
        "outcomes_consulted": False,
        "protocol_sha256": protocol["protocol_sha256"],
        "input_artifacts": sorted(inputs.values()),
        "input_sha256": {
            value: _file_sha256(root / value) for value in sorted(inputs.values())
        },
        "repositories": selected,
        "runs": runs,
    }
    result["plan_sha256"] = canonical_sha256(result)
    return result


def validate_pilot_plan(
    plan: dict[str, Any], protocol: dict[str, Any], root: Path
) -> list[str]:
    errors: list[str] = []
    if plan.get("outcomes_consulted") is not False:
        errors.append("pilot plan must remain outcome blind")
    forbidden = set(protocol["leakage_controls"]["forbidden_inputs"])
    if forbidden.intersection(plan.get("input_artifacts", [])):
        errors.append("pilot plan uses forbidden outcome input")
    repositories = plan.get("repositories", [])
    counts = Counter(item.get("stratum") for item in repositories)
    if counts != Counter(PILOT_STRATA):
        errors.append("pilot plan must contain five repositories per stratum")
    repository_ids = [item.get("canonical_repository_id") for item in repositories]
    if len(repository_ids) != len(set(repository_ids)):
        errors.append("repository appears in more than one pilot stratum")
    runs = plan.get("runs", [])
    if len(runs) != 100 or len({item.get("run_id") for item in runs}) != 100:
        errors.append("pilot plan must contain 100 unique runs")
    run_pairs = Counter(
        (item.get("canonical_repository_id"), item.get("prompt_family_id"))
        for item in runs
    )
    if any(count != 1 for count in run_pairs.values()) or len(run_pairs) != 100:
        errors.append("each repository must have each prompt family exactly once")
    for path, expected in plan.get("input_sha256", {}).items():
        resolved = root / path
        if not resolved.exists() or _file_sha256(resolved) != expected:
            errors.append(f"input SHA-256 mismatch: {path}")
    if canonical_sha256(plan) != plan.get("plan_sha256"):
        errors.append("plan_sha256 does not match canonical content")
    return errors


def load_pilot_protocol(path: Path, root: Path) -> dict[str, Any]:
    protocol = _read(path)
    errors = []
    if [item.get("id") for item in protocol.get("prompt_families", [])] != PROMPT_IDS:
        errors.append("protocol must freeze exactly five ordered prompt families")
    if (
        protocol.get("deep_search", {}).get("may_directly_populate_analytic_fields")
        is not False
    ):
        errors.append("Deep Search may not directly populate analytic fields")
    if protocol.get("analytic_admission_routes") != sorted(ADMISSIBLE_ROUTES):
        errors.append("analytic admission routes do not match locked routes")
    if protocol.get("outcomes_consulted") is not False:
        errors.append("protocol must remain outcome blind")
    for path, expected in protocol.get("input_sha256", {}).items():
        resolved = root / path
        if not resolved.exists() or _file_sha256(resolved) != expected:
            errors.append(f"protocol input SHA-256 mismatch: {path}")
    if canonical_sha256(protocol) != protocol.get("protocol_sha256"):
        errors.append("protocol_sha256 does not match canonical content")
    if errors:
        raise SemanticTopologyProtocolError("; ".join(errors))
    return protocol


def validate_semantic_change_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field_name in ANALYTIC_FIELDS:
        field = record.get(field_name, {})
        status = field.get("status")
        routes = set(field.get("evidence_routes", []))
        if status == "observed" and not routes.intersection(ADMISSIBLE_ROUTES):
            errors.append(
                "observed analytic fields require an admissible verification route"
            )
        if status == "unavailable":
            if not field.get("reason"):
                errors.append("unavailable fields require a non-empty reason")
            extra = set(field) - {"status", "reason", "evidence_routes"}
            if extra:
                errors.append("unavailable fields cannot contain analytic values")
    if record.get("provenance", {}).get("outcomes_consulted") is not False:
        errors.append("record provenance must remain outcome blind")
    return sorted(set(errors))


def write_frozen_artifacts(root: Path) -> None:
    protocol_path = root / "study" / "semantic-topology-protocol.v1.json"
    protocol = _read(protocol_path)
    protocol["input_sha256"] = {
        path: _file_sha256(root / path)
        for path in sorted(protocol["input_artifacts"].values())
    }
    protocol["protocol_sha256"] = canonical_sha256(protocol)
    protocol_path.write_text(json.dumps(protocol, indent=2) + "\n")
    plan = build_pilot_plan(protocol, root)
    (root / "study" / "semantic-topology-pilot-plan.v1.json").write_text(
        json.dumps(plan, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        write_frozen_artifacts(args.root.resolve())
    else:
        protocol = load_pilot_protocol(
            args.root / "study" / "semantic-topology-protocol.v1.json",
            args.root,
        )
        plan = _read(args.root / "study" / "semantic-topology-pilot-plan.v1.json")
        errors = validate_pilot_plan(plan, protocol, args.root)
        if errors:
            raise SystemExit("; ".join(errors))


if __name__ == "__main__":
    main()
