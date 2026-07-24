"""Detect agent fingerprints in a commit message.

We match the structured trailers and attribution lines that coding agents add to
commit messages. This is deliberately a LOWER BOUND: most agent-written code is
never labeled, and labeling conventions themselves appeared over time. We only
count signatures that nothing but an agent would produce, so every match is a
true positive — the share we compute under-counts magnitude but never invents
attribution. Patterns are listed openly so anyone can audit or extend them.
"""
from __future__ import annotations

import re

# agent label -> list of compiled patterns (case-insensitive).
# Each pattern targets a signature only an agent emits (trailer or attribution line).
AGENT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "claude_code": (
        re.compile(r"co-authored-by:\s*claude\b", re.I),
        re.compile(r"noreply@anthropic\.com", re.I),
        re.compile(r"generated with\s*\[?claude code", re.I),
        re.compile(r"claude\.(?:ai/code|com/claude-code)", re.I),
    ),
    "copilot": (
        re.compile(r"co-authored-by:\s*copilot\b", re.I),
        re.compile(r"copilot-swe-agent", re.I),
        re.compile(r"@copilot\.github\.com", re.I),
    ),
    "devin": (
        re.compile(r"co-authored-by:\s*devin\b", re.I),
        re.compile(r"devin-ai-integration", re.I),
    ),
    "cursor": (
        re.compile(r"co-authored-by:\s*cursor(?:\s*agent)?\b", re.I),
        re.compile(r"cursoragent", re.I),
    ),
    "codex": (
        re.compile(r"co-authored-by:\s*(?:openai\s*)?codex\b", re.I),
    ),
}


def match_agents(message: str) -> frozenset[str]:
    """Return the set of agent labels whose signature appears in the message."""
    if not message:
        return frozenset()
    return frozenset(
        label for label, patterns in AGENT_PATTERNS.items()
        if any(p.search(message) for p in patterns)
    )


def is_agent_signed(message: str) -> bool:
    return bool(match_agents(message))
