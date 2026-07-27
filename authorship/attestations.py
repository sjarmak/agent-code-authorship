"""Semantic validation for authorship attestation responses."""
from __future__ import annotations

import re
from typing import Any


COMMIT = re.compile(r"^[0-9a-f]{40}$")
DURABLE_EVIDENCE = {"public_github_comment", "signed_statement"}
DISPOSITIONS = {"pending", "eligible", "rejected", "declined", "no_response"}


def validate_ledger(
    ledger: dict[str, Any], candidate_frame: dict[str, Any]
) -> list[str]:
    errors = []
    if ledger.get("ledger_version") != 2:
        errors.append("ledger_version must equal 2")
    status = ledger.get("status")
    if status not in (
        "open_for_responses",
        "frozen_for_modeling",
        "frozen_without_solicitation",
    ):
        errors.append("ledger has unsupported status")
    if status == "frozen_without_solicitation":
        closure = ledger.get("closure", {})
        if closure.get("kind") != "no_external_outreach":
            errors.append("no-solicitation ledger requires no_external_outreach closure")
        if closure.get("applies_to_all_unsolicited_candidates") is not True:
            errors.append("closure must apply to all unsolicited candidates")
        if not closure.get("decision_at"):
            errors.append("no-solicitation closure requires decision_at")
        if closure.get("label_inference_from_closure") is not False:
            errors.append("no-solicitation closure must not infer labels")
    candidates = {
        candidate["id"].lower(): candidate
        for candidate in candidate_frame["candidates"]
    }
    records = ledger.get("records")
    if not isinstance(records, list):
        return errors + ["records must be a list"]
    seen = set()
    for index, record in enumerate(records):
        prefix = f"records[{index}]"
        repo_id = record.get("repository")
        canonical = repo_id.lower() if isinstance(repo_id, str) else ""
        candidate = candidates.get(canonical)
        if candidate is None:
            errors.append(f"{prefix} repository is not in the frozen frame")
            continue
        if canonical in seen:
            errors.append(f"{repo_id} has multiple ledger records")
        seen.add(canonical)
        disposition = record.get("disposition")
        if disposition not in DISPOSITIONS:
            errors.append(f"{repo_id} has unsupported disposition")
        if record.get("label") != candidate["proposed_label"]:
            errors.append(f"{repo_id} changes the frozen candidate label")
        if disposition != "eligible":
            continue
        attestor = record.get("attestor", {})
        if not attestor.get("identity") or not attestor.get("authority"):
            errors.append(f"{repo_id} eligible attestation lacks identity or authority")
        evidence = record.get("evidence", {})
        if evidence.get("kind") not in DURABLE_EVIDENCE or not (
            evidence.get("url") or evidence.get("sha256")
        ):
            errors.append(f"{repo_id} eligible evidence must be durable and locatable")
        scope = record.get("scope", {})
        commits = scope.get("commits")
        if (
            scope.get("kind") not in ("commit_set", "entire_history_through_commit")
            or not isinstance(commits, list)
            or not commits
            or any(not isinstance(commit, str) or not COMMIT.fullmatch(commit) for commit in commits)
        ):
            errors.append(
                f"{repo_id} scope requires full 40-character commit identifiers"
            )
        claims = record.get("claims", {})
        if record["label"] == "human":
            if claims.get("no_ai_or_llm_assistance") is not True:
                errors.append(
                    f"{repo_id} human claim must explicitly deny AI or LLM assistance"
                )
            if scope.get("kind") != "commit_set":
                errors.append(f"{repo_id} human evidence requires an exact commit set")
            if scope.get("code_and_tests") is not True:
                errors.append(f"{repo_id} human scope must cover code and tests")
            if record.get("basis") not in (
                "personal_authorship",
                "direct_maintainer_review",
            ):
                errors.append(f"{repo_id} has unsupported human knowledge basis")
        elif claims.get("agent_generated_code") is not True:
            errors.append(f"{repo_id} agent claim must explicitly attest generation")
    return errors
