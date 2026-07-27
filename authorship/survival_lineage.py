"""Conservative line-state mapping across merge-to-horizon Git diffs."""
from __future__ import annotations

import difflib
import re
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any


HUNK = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def select_horizon(
    timeline: list[tuple[str, str]],
    *,
    merge_commit: str,
    merged_at: str,
    days: int,
    cutoff: str,
) -> dict[str, Any]:
    target = _timestamp(merged_at) + timedelta(days=days)
    if target > _timestamp(cutoff):
        return {
            "status": "right_censored",
            "horizon_days": days,
            "target_at": target.isoformat().replace("+00:00", "Z"),
        }
    positions = {commit: index for index, (commit, _date) in enumerate(timeline)}
    merge_position = positions.get(merge_commit)
    if merge_position is None:
        return {
            "status": "unobservable",
            "horizon_days": days,
            "reason": "merge_commit_absent_from_first_parent",
        }
    eligible = [
        (index, commit, committed_at)
        for index, (commit, committed_at) in enumerate(timeline)
        if index >= merge_position and _timestamp(committed_at) <= target
    ]
    if not eligible:
        return {
            "status": "unobservable",
            "horizon_days": days,
            "reason": "no_descendant_snapshot_by_horizon",
        }
    _index, commit, committed_at = eligible[-1]
    return {
        "status": "observed",
        "horizon_days": days,
        "target_at": target.isoformat().replace("+00:00", "Z"),
        "commit": commit,
        "committed_at": committed_at,
    }


def _path(token: str) -> str | None:
    try:
        token = shlex.split(token)[0]
    except (IndexError, ValueError):
        return None
    if token == "/dev/null":
        return None
    return token[2:] if token.startswith(("a/", "b/")) else token


def parse_patch(patch: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    change: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    old_line = new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            try:
                parts = shlex.split(raw)
            except ValueError:
                parts = ["diff", "--git", "", ""]
            change = {
                "old_path": _path(parts[2]),
                "new_path": _path(parts[3]),
                "binary": False,
                "hunks": [],
            }
            changes.append(change)
            hunk = None
        elif change is None:
            continue
        elif raw.startswith("--- "):
            change["old_path"] = _path(raw[4:])
        elif raw.startswith("+++ "):
            change["new_path"] = _path(raw[4:])
        elif raw.startswith("rename from "):
            change["old_path"] = raw.removeprefix("rename from ")
        elif raw.startswith("rename to "):
            change["new_path"] = raw.removeprefix("rename to ")
        elif raw.startswith(("Binary files ", "GIT binary patch")):
            change["binary"] = True
        elif match := HUNK.match(raw):
            hunk = {
                "old_start": int(match.group("old")),
                "old_count": int(match.group("old_count") or 1),
                "new_start": int(match.group("new")),
                "new_count": int(match.group("new_count") or 1),
                "removed": [],
                "added": [],
            }
            change["hunks"].append(hunk)
            old_line = hunk["old_start"]
            new_line = hunk["new_start"]
        elif hunk is not None and raw.startswith("-") and not raw.startswith("---"):
            hunk["removed"].append({"line_number": old_line, "text": raw[1:]})
            old_line += 1
        elif hunk is not None and raw.startswith("+") and not raw.startswith("+++"):
            hunk["added"].append({"line_number": new_line, "text": raw[1:]})
            new_line += 1
        elif hunk is not None and not raw.startswith("\\"):
            old_line += 1
            new_line += 1
    return changes


def parse_name_status(payload: str) -> dict[str, set[str]]:
    commits: dict[str, set[str]] = {}
    current: str | None = None
    for raw in payload.splitlines():
        if raw.startswith("__COMMIT__"):
            current = raw.removeprefix("__COMMIT__")
            commits.setdefault(current, set())
            continue
        if current is None or not raw:
            continue
        parts = raw.split("\t")
        if len(parts) >= 2:
            commits[current].update(parts[1:])
    return commits


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left.strip(), right.strip()).ratio()


def classify_lines(
    introductions: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    *,
    threshold: float = 0.8,
    margin: float = 0.1,
) -> list[dict[str, Any]]:
    by_path = {change["old_path"]: change for change in changes}
    global_additions = [
        (other["new_path"], candidate)
        for other in changes
        if other["new_path"] is not None
        for hunk in other["hunks"]
        for candidate in hunk["added"]
    ]
    exact_global: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for candidate_path, candidate in global_additions:
        exact_global.setdefault(candidate["text"], []).append(
            (candidate_path, candidate)
        )
    results = []
    for introduction in introductions:
        path = introduction["path"]
        line_number = introduction["line_number"]
        text = introduction["text"]
        change = by_path.get(path)
        if change is None:
            results.append(
                {
                    "state": "unchanged",
                    "horizon_path": path,
                    "horizon_line_number": line_number,
                }
            )
            continue
        if change["binary"] or change["new_path"] is None:
            state = "unobservable" if change["binary"] else "deleted"
            results.append(
                {
                    "state": state,
                    "reason": "binary_change" if change["binary"] else "file_deleted",
                }
            )
            continue
        containing = next(
            (
                hunk
                for hunk in change["hunks"]
                if hunk["old_count"] > 0
                and hunk["old_start"]
                <= line_number
                < hunk["old_start"] + hunk["old_count"]
            ),
            None,
        )
        if containing is None:
            shift = sum(
                hunk["new_count"] - hunk["old_count"]
                for hunk in change["hunks"]
                if hunk["old_start"] <= line_number
            )
            results.append(
                {
                    "state": "unchanged",
                    "horizon_path": change["new_path"],
                    "horizon_line_number": line_number + shift,
                }
            )
            continue
        additions = containing["added"]
        if not additions:
            exact_matches = exact_global.get(text, [])
            if len(exact_matches) == 1:
                candidate_path, candidate = exact_matches[0]
                results.append(
                    {
                        "state": "unchanged",
                        "horizon_path": candidate_path,
                        "horizon_line_number": candidate["line_number"],
                    }
                )
            elif global_additions:
                if len(global_additions) > 2000:
                    results.append(
                        {
                            "state": "unobservable",
                            "reason": "cross_file_candidate_space_too_broad",
                        }
                    )
                    continue
                scored_global = sorted(
                    (
                        (_similarity(text, candidate["text"]), candidate_path, candidate)
                        for candidate_path, candidate in global_additions
                    ),
                    key=lambda item: (-item[0], item[1], item[2]["line_number"]),
                )
                best_score, candidate_path, candidate = scored_global[0]
                runner_up = scored_global[1][0] if len(scored_global) > 1 else 0.0
                if best_score >= threshold and best_score - runner_up >= margin:
                    results.append(
                        {
                            "state": "modified_candidate",
                            "horizon_path": candidate_path,
                            "horizon_line_number": candidate["line_number"],
                            "horizon_text": candidate["text"],
                            "structural_score": round(best_score, 6),
                        }
                    )
                else:
                    results.append(
                        {
                            "state": "unobservable",
                            "reason": "potential_cross_file_move",
                            "best_structural_score": round(best_score, 6),
                            "horizon_path": candidate_path,
                            "horizon_line_number": candidate["line_number"],
                            "horizon_text": candidate["text"],
                        }
                    )
            else:
                results.append({"state": "deleted", "reason": "line_removed"})
            continue
        exact = [candidate for candidate in additions if candidate["text"] == text]
        if len(exact) == 1:
            results.append(
                {
                    "state": "unchanged",
                    "horizon_path": change["new_path"],
                    "horizon_line_number": exact[0]["line_number"],
                }
            )
            continue
        scored = sorted(
            (
                (_similarity(text, candidate["text"]), candidate)
                for candidate in additions
            ),
            key=lambda item: (-item[0], item[1]["line_number"]),
        )
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= threshold and best_score - runner_up >= margin:
            results.append(
                {
                    "state": "modified_candidate",
                    "horizon_path": change["new_path"],
                    "horizon_line_number": best["line_number"],
                    "horizon_text": best["text"],
                    "structural_score": round(best_score, 6),
                }
            )
        else:
            results.append(
                {
                    "state": "unobservable",
                    "reason": (
                        "ambiguous_replacement"
                        if best_score >= threshold
                        else "replacement_below_threshold"
                    ),
                    "best_structural_score": round(best_score, 6),
                    "horizon_path": change["new_path"],
                    "horizon_line_number": best["line_number"],
                    "horizon_text": best["text"],
                }
            )
    return results


def advance_states(
    states: dict[str, dict[str, Any]],
    changes: list[dict[str, Any]],
    *,
    commit: str,
    active_index: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    if active_index is None:
        active_index = {}
        for line_id, state in states.items():
            if state["state"] not in {"deleted", "unobservable"}:
                active_index.setdefault(state["path"], set()).add(line_id)
    original_index = {
        path: sorted(line_ids) for path, line_ids in active_index.items() if line_ids
    }
    events = []
    for change in changes:
        path = change["old_path"]
        entries = [
            (line_id, states[line_id])
            for line_id in original_index.get(path, [])
        ]
        if not entries:
            continue
        introductions = [
            {
                "path": state["path"],
                "line_number": state["line_number"],
                "text": state["text"],
            }
            for _line_id, state in entries
        ]
        decisions = classify_lines(introductions, changes)
        for (line_id, state), decision in zip(entries, decisions, strict=True):
            prior_text = state["text"]
            outcome = decision["state"]
            if outcome == "unchanged":
                destination = decision["horizon_path"]
                if destination != path:
                    active_index[path].discard(line_id)
                    active_index.setdefault(destination, set()).add(line_id)
                state["path"] = decision["horizon_path"]
                state["line_number"] = decision["horizon_line_number"]
                continue
            event = {
                "line_id": line_id,
                "commit": commit,
                "decision": outcome,
                "prior_text": prior_text,
                **{key: value for key, value in decision.items() if key != "state"},
            }
            events.append(event)
            if outcome == "modified_candidate":
                destination = decision["horizon_path"]
                if destination != path:
                    active_index[path].discard(line_id)
                    active_index.setdefault(destination, set()).add(line_id)
                state["state"] = "modified_candidate"
                state["path"] = decision["horizon_path"]
                state["line_number"] = decision["horizon_line_number"]
                state["text"] = decision["horizon_text"]
                state["last_modified_commit"] = commit
            else:
                active_index[path].discard(line_id)
                state["state"] = outcome
                state["terminal_commit"] = commit
                state["terminal_reason"] = decision.get("reason")
    return events
