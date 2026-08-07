"""Render the semantic-topology pilot report from checksummed artifacts."""

from __future__ import annotations

from collections import Counter
from typing import Any


def _gate_row(label: str, gate: dict[str, Any]) -> str:
    status = "PASS" if gate.get("passes") else "NOT IDENTIFIED"
    return f"| {label} | {status} |"


def render_report(
    *,
    protocol: dict[str, Any],
    plan: dict[str, Any],
    deep_inventory: dict[str, Any],
    record_inventory: dict[str, Any],
    candidate_inventory: dict[str, Any],
    candidate_verification: dict[str, Any],
    control_candidates: dict[str, Any],
    gates: dict[str, Any],
    terminal_failures: list[dict[str, Any]] | None = None,
    field_status_counts: dict[str, dict[str, int]] | None = None,
    record_summary: dict[str, Any] | None = None,
    run_summary: dict[str, Any] | None = None,
) -> str:
    """Return a report whose quantitative claims resolve to input artifacts."""
    gate_values = gates["gates"]
    passing_fields = gate_values["reliable_topology_fields"].get("passing_fields", [])
    hashes = [
        ("Protocol", protocol["protocol_sha256"]),
        ("Pilot plan", plan["plan_sha256"]),
        ("Deep Search inventory", deep_inventory["inventory_sha256"]),
        ("Semantic record inventory", record_inventory["inventory_sha256"]),
        ("Candidate inventory", candidate_inventory["inventory_sha256"]),
        ("Candidate verification", candidate_verification["verification_sha256"]),
        ("Held-out control candidates", control_candidates["artifact_sha256"]),
        ("Utility gates", gates["result_sha256"]),
    ]
    control_status_counts = Counter(
        item["status"]
        for item in control_candidates.get("source_repository_statuses", [])
    )
    record_summary = record_summary or {}
    run_summary = run_summary or {}
    lines = [
        "# Sourcegraph semantic change-topology pilot",
        "",
        "## Result",
        "",
        f"Pilot result: **{gates['pilot_result']}**.",
        "",
        "Deep Search hypotheses are not statistical labels. All reported analytic "
        "fields below enter through pinned Git, deterministic Sourcegraph evidence, "
        "or frozen blinded adjudication.",
        "",
        "## Frozen execution",
        "",
        f"- Repositories: {len(plan['repositories'])}",
        f"- Planned investigations: {len(plan['runs'])}",
        "- Materialized investigations: "
        f"{deep_inventory['materialized_run_count']} / "
        f"{deep_inventory['planned_run_count']}",
        f"- Terminal statuses: `{deep_inventory['counts']}`",
        "- Conversation identities preserved: "
        f"{run_summary.get('conversation_identity_present_count', 'unavailable')}",
        "- Raw answers preserved: "
        f"{run_summary.get('raw_answer_present_count', 'unavailable')}",
        "- Search-trace statuses: "
        f"`{run_summary.get('search_trace_status_counts', {})}`",
        "- Model-metadata statuses: "
        f"`{run_summary.get('model_metadata_status_counts', {})}`",
        f"- Cited files extracted: {run_summary.get('cited_file_count', 'unavailable')}",
        "- Proposed deterministic queries extracted: "
        f"{run_summary.get('proposed_query_count', 'unavailable')}",
        f"- Semantic hunk records: {record_inventory['record_count']}",
        f"- Record materialization failures: {record_inventory['failure_count']}",
        f"- Paths by class: `{record_summary.get('path_class_counts', {})}`",
        "- Records with subsequent same-path changes: "
        f"{record_summary.get('subsequent_change_present_count', 'unavailable')}",
        "- Records whose first follow-up uses a different commit identity: "
        f"{record_summary.get('first_followup_author_differs_count', 'unavailable')}",
        "- Distinct follow-up-author count distribution: "
        f"`{record_summary.get('distinct_followup_author_count_distribution', {})}`",
        "",
        "## Evidence boundary",
        "",
        f"- Deep Search candidates: {candidate_inventory['candidate_count']}",
        f"- Pinned-Git verified citations: "
        f"{candidate_verification['verified_count']}",
        f"- Unavailable citations: {candidate_verification['unavailable_count']}",
        f"- Query candidates awaiting separate execution: "
        f"{candidate_verification['candidate_only_count']}",
        f"- Repository-held-out control candidates: "
        f"{control_candidates['candidate_count']}",
        f"- Control-source statuses: `{dict(sorted(control_status_counts.items()))}`",
        *[
            f"- Field `{field}`: "
            + ", ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
            for field, status_counts in sorted((field_status_counts or {}).items())
        ],
        f"- Terminal investigation failures: {len(terminal_failures or [])}",
        *[
            "- Failure: "
            f"`{failure['canonical_repository_id']}` / "
            f"`{failure['prompt_family_id']}` / "
            f"`{failure['error_code']}`"
            for failure in (terminal_failures or [])
        ],
        "",
        "Unavailable SCIP, ownership, history, or citation evidence remains typed "
        "missingness; it is never silently imputed.",
        "",
        "## Preregistered utility gates",
        "",
        "| Gate | Result |",
        "|---|---|",
        _gate_row("New discovery family", gate_values["new_discovery_family"]),
        _gate_row("Task-matched controls", gate_values["task_matched_controls"]),
        _gate_row("Reliable topology fields", gate_values["reliable_topology_fields"]),
        "",
        "Reliable fields: "
        + (", ".join(f"`{field}`" for field in passing_fields) or "none"),
        "",
        "## Limitations",
        "",
        "- Deep Search may omit its internal search trace and model metadata; those "
        "API omissions are preserved explicitly.",
        "- Different commit identities are not interpreted as human authorship.",
        "- Candidate semantic controls remain unlabeled until deterministic "
        "eligibility checks and blinded review succeed.",
        "- Reliability reviewers were independent blinded agents, not humans; "
        "their agreement is reported as reproducibility evidence over pinned Git.",
        "- Distinct author counts describe normalized Git name/email pairs, not "
        "people; mailmap or identity-resolution claims are intentionally excluded.",
        "- A passed topology-field gate establishes reliable enrichment fields, "
        "not a general agent-code detector.",
        "",
        "## Artifact hashes",
        "",
        "| Artifact | SHA-256 |",
        "|---|---|",
        *[f"| {label} | `{digest}` |" for label, digest in hashes],
        "",
        "## Exact reproduction",
        "",
        "```bash",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_protocol",
        "export SRC_ENDPOINT=https://demo.sourcegraph.com",
        "export SRC_ACCESS_TOKEN='<Sourcegraph access token>'",
        "for shard in 0 1 2 3; do",
        "  PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search \\",
        '    --shard-index "$shard" --shard-count 4 &',
        "done",
        "wait",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search "
        "--retry-error-code transport_error",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search --limit 0",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search "
        "--validate-only",
        "PYTHONPATH=. python3 -m authorship.semantic_change_materialization",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_finalize",
        "PYTHONPATH=. python3 -m authorship.semantic_topology_audit",
        "PYTHONPATH=. pytest -q tests/test_semantic_topology_*.py "
        "tests/test_semantic_change_materialization.py",
        "python3 -m ruff check authorship/semantic_topology_*.py "
        "authorship/semantic_change_materialization.py",
        "```",
        "",
    ]
    return "\n".join(lines)
