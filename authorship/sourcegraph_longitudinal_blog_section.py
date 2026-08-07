"""Render the frozen Sourcegraph longitudinal coverage audit."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

from authorship.sourcegraph_longitudinal_materialization import (
    MATERIALIZATION_VERSION,
    materialization_sha256,
)

EXPECTED_UNIT_COUNT = 96
EXPECTED_MATERIALIZED_COUNT = 93
EXPECTED_MISSING_REPOSITORIES = frozenset(
    {
        "axinc-ai/ailia-models",
        "disler/single-file-agents",
        "reflex-dev/reflex-web",
    }
)


def _validated_missing_repositories(document: Mapping[str, Any]) -> list[str]:
    if document.get("materialization_sha256") != materialization_sha256(document):
        raise ValueError("longitudinal materialization checksum does not match")
    units = document.get("units")
    if not isinstance(units, list) or any(
        not isinstance(unit, Mapping) for unit in units
    ):
        raise ValueError("longitudinal materialization units are invalid")
    invalid = [unit for unit in units if unit.get("valid") is False]
    missing = {
        unit.get("canonical_repository_id")
        for unit in invalid
        if unit.get("failure_stage") == "sourcegraph_probe"
    }
    contract = (
        document.get("materialization_version") == MATERIALIZATION_VERSION,
        document.get("outcomes_consulted") is False,
        document.get("unit_count") == EXPECTED_UNIT_COUNT == len(units),
        document.get("materialized_unit_count") == EXPECTED_MATERIALIZED_COUNT,
        document.get("invalid_unit_count") == len(invalid) == 3,
        document.get("attempted_unit_count") == EXPECTED_MATERIALIZED_COUNT,
        document.get("skipped_probe_unit_count") == len(invalid),
        missing == EXPECTED_MISSING_REPOSITORIES,
    )
    if not all(contract):
        raise ValueError("longitudinal materialization contract is invalid")
    return sorted(missing)


def render_longitudinal_section(document: Mapping[str, Any]) -> str:
    """Render an accessible coverage split from the frozen execution artifact."""
    missing = _validated_missing_repositories(document)
    repository_list = ", ".join(f"<code>{escape(name)}</code>" for name in missing)
    return f"""
  <figure>
    <div class="figure-head">
      <h3>Longitudinal verification coverage</h3>
      <span>Frozen 96-repository survival frame</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 420" role="img"
           aria-labelledby="longitudinal-coverage-title longitudinal-coverage-desc">
        <title id="longitudinal-coverage-title">Sourcegraph and pinned Git longitudinal coverage</title>
        <desc id="longitudinal-coverage-desc">Of 96 frozen repositories, 93 have checksummed Sourcegraph probe and longitudinal shards and 3 are explicit Sourcegraph absences. Pinned Git event histories cover all 96.</desc>
        <rect width="1120" height="420" fill="oklch(98% 0.006 83)"/>
        <text x="50" y="54" font-size="18" font-weight="700"
              fill="oklch(22% 0.026 264)">Two evidence layers, two denominators</text>
        <text x="50" y="96" font-size="14" fill="oklch(48% 0.025 260)">
          Sourcegraph verifies indexed revision, diff result, and blame provenance.
        </text>
        <rect x="50" y="130" width="970" height="74" rx="8"
              fill="oklch(90% 0.035 272)"/>
        <rect x="50" y="130" width="940" height="74" rx="8"
              fill="oklch(48% 0.2 292)"/>
        <text x="72" y="176" font-size="25" font-weight="750"
              fill="oklch(98% 0.006 83)">93 checksummed longitudinal shards</text>
        <circle cx="62" cy="262" r="10" fill="oklch(65% 0.18 46)"/>
        <text x="86" y="269" font-size="18" font-weight="700"
              fill="oklch(22% 0.026 264)">
          3 absent from Sourcegraph
        </text>
        <text x="50" y="335" font-size="14" fill="oklch(48% 0.025 260)">
          Pinned Git is authoritative for lineage and remains available when index evidence is missing.
        </text>
        <rect x="50" y="355" width="970" height="36" rx="8"
              fill="oklch(30% 0.035 260)"/>
        <text x="72" y="380" font-size="18"
              font-weight="700" fill="oklch(98% 0.006 83)">
          96/96 pinned Git histories
        </text>
      </svg>
    </div>
    <figcaption>
      <strong>93 checksummed longitudinal shards.</strong>
      <strong>3 explicit Sourcegraph absences.</strong>
      <strong>Pinned Git survival population: 96/96.</strong>
      The
      unavailable repositories are {repository_list}. They remain missing
      Sourcegraph evidence, not negative findings and not dropped survival
      observations. The audit is frozen in
      <code>study/sourcegraph-longitudinal-materialization.v3.json</code>.
    </figcaption>
  </figure>
"""
