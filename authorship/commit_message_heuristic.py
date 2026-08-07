"""Deterministic commit-message style heuristic for exploratory authorship analysis."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

MINIMUM_BASELINE_COMMITS = 30
BASELINE_QUANTILE = 0.9

_PROVENANCE_TRAILER = re.compile(
    r"(?im)^(?:co-authored-by|coauthored-by|generated-by|assisted-by|"
    r"ai-generated-by|ai-assisted-by|signed-off-by):[^\n]*(?:\n|$)"
)
_AGENT_IDENTITY = re.compile(
    r"(?i)\b(?:claude(?:\s+(?:code|opus|sonnet|haiku))?|chatgpt|codex|"
    r"github\s+copilot|copilot|cursor|devin|gemini|openai|anthropic)\b"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL = re.compile(r"https?://\S+")
_HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_-]+")
_ISSUE_REFERENCE = re.compile(r"(?<!\w)#\d+\b")
_REVISION = re.compile(r"(?i)\b[0-9a-f]{7,40}\b")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_TERMINAL_SENTENCE = re.compile(r"[.!?](?=(?:[\"')\]]*)?(?:\s|$))")
_VALIDATION = re.compile(
    r"(?ix)\b(?:"
    r"(?:ran|run|running|passes?|passed|passing|green|verified?|validated?)"
    r"(?:\s+\w+){0,5}\s+(?:tests?|checks?|pytest|go\s+test|cargo\s+test|"
    r"lint(?:er|ing)?|type[- ]?checks?|build)"
    r"|(?:tests?|checks?|pytest|go\s+test|cargo\s+test|lint(?:er|ing)?|"
    r"type[- ]?checks?|build)(?:\s+\w+){0,5}\s+"
    r"(?:ran|run|passes?|passed|passing|green|succeeded|completed|verified?)"
    r")\b"
)


def normalize_message(message: str) -> str:
    """Remove direct provenance markers while preserving prose structure."""
    if not isinstance(message, str):
        raise TypeError("message must be a string")
    normalized = _PROVENANCE_TRAILER.sub("", message.replace("\r\n", "\n"))
    normalized = _AGENT_IDENTITY.sub("", normalized)
    normalized = _EMAIL.sub("", normalized)
    normalized = _URL.sub("", normalized)
    normalized = _HANDLE.sub("", normalized)
    normalized = _ISSUE_REFERENCE.sub("", normalized)
    normalized = _REVISION.sub("", normalized)
    lines = tuple(line.rstrip() for line in normalized.splitlines())
    compacted = tuple(
        line
        for index, line in enumerate(lines)
        if line.strip() or index == 0 or lines[index - 1].strip()
    )
    return "\n".join(compacted).strip()


def _paragraph_count(lines: Sequence[str]) -> int:
    count = 0
    inside = False
    for line in lines:
        if line.strip():
            if not inside:
                count += 1
            inside = True
        else:
            inside = False
    return count


def extract_style_features(message: str) -> dict[str, Any]:
    """Extract the preregistered mechanical style features."""
    normalized = normalize_message(message)
    subject, separator, body = normalized.partition("\n")
    body = body.lstrip("\n") if separator else ""
    body_lines = tuple(body.splitlines())
    nonblank_lines = tuple(line for line in body_lines if line.strip())
    prose_lines = tuple(line for line in nonblank_lines if not _BULLET.match(line))
    sentence_count = len(_TERMINAL_SENTENCE.findall(body))
    terminal_line_count = sum(
        bool(re.search(r"[.!?](?:[\"')\]]*)$", line.strip())) for line in prose_lines
    )
    sentence_line_rate = (
        terminal_line_count / len(prose_lines) if prose_lines else 0.0
    )
    bullet_count = sum(bool(_BULLET.match(line)) for line in nonblank_lines)
    paragraph_count = _paragraph_count(body_lines)
    body_word_count = len(re.findall(r"\b[\w'-]+\b", body))
    return {
        "normalized_message_sha256": hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest(),
        "subject_word_count": len(re.findall(r"\b[\w'-]+\b", subject)),
        "body_word_count": body_word_count,
        "body_nonblank_line_count": len(nonblank_lines),
        "paragraph_count": paragraph_count,
        "bullet_count": bullet_count,
        "sentence_count": sentence_count,
        "sentence_line_rate": sentence_line_rate,
        "structure_count": paragraph_count + bullet_count + sentence_count,
        "explicit_validation": bool(_VALIDATION.search(body)),
    }


def _nearest_rank_quantile(values: Sequence[int], quantile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return float(ordered[rank - 1])


def baseline_from_messages(
    messages: Sequence[str],
    *,
    repository_id: str,
    minimum_commits: int = MINIMUM_BASELINE_COMMITS,
) -> dict[str, Any]:
    """Build an unfitted repository-local baseline from pre-adoption messages."""
    features = tuple(
        extract_style_features(message)
        for message in messages
        if normalize_message(message)
    )
    if len(features) < minimum_commits:
        return {
            "status": "unavailable",
            "repository_id": repository_id,
            "commit_count": len(features),
            "minimum_commit_count": minimum_commits,
        }
    return {
        "status": "available",
        "repository_id": repository_id,
        "commit_count": len(features),
        "minimum_commit_count": minimum_commits,
        "quantile": BASELINE_QUANTILE,
        "body_word_count_p90": _nearest_rank_quantile(
            [feature["body_word_count"] for feature in features],
            BASELINE_QUANTILE,
        ),
        "structure_count_p90": _nearest_rank_quantile(
            [feature["structure_count"] for feature in features],
            BASELINE_QUANTILE,
        ),
    }


def classify_message(
    message: str,
    *,
    baseline: Mapping[str, Any] | None = None,
    thresholds: Sequence[int] = (3, 4, 5),
) -> dict[str, Any]:
    """Classify one message using the frozen point rule."""
    features = extract_style_features(message)
    signals = {
        "verbose_body": features["body_word_count"] >= 80,
        "structured_body": (
            features["body_nonblank_line_count"] >= 4
            and features["paragraph_count"] >= 2
        ),
        "enumerated_detail": features["bullet_count"] >= 3,
        "sentence_form": (
            features["sentence_count"] >= 4
            and features["sentence_line_rate"] >= 0.5
        ),
        "explicit_validation": features["explicit_validation"],
        "local_length_outlier": False,
        "local_structure_outlier": False,
    }
    if baseline and baseline.get("status") == "available":
        signals = {
            **signals,
            "local_length_outlier": (
                features["body_word_count"] > baseline["body_word_count_p90"]
            ),
            "local_structure_outlier": (
                features["structure_count"] > baseline["structure_count_p90"]
            ),
        }
    active_signals = tuple(name for name, active in signals.items() if active)
    score = len(active_signals)
    return {
        "features": features,
        "signals": list(active_signals),
        "score": score,
        "classifications": {
            f"threshold_{threshold}": score >= threshold for threshold in thresholds
        },
    }
