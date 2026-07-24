#!/usr/bin/env python3
"""Corpus loading and label-set construction for the fingerprint.

One gather pass yields four disjoint populations, and every downstream claim
depends on which of them a number came from:

  SIGNED    cohort hunks, era >= AGENT_ERA, trailer-signed  -> agent, certain
  UNSIGNED  cohort hunks, era >= AGENT_ERA, no trailer      -> UNKNOWN, the
                                                               population to
                                                               quantify
  PRE       cohort hunks, era <  PRE_ERA                     -> human, certain
                                                               (agents did not
                                                               exist)
  OWN       maintainer's own repos, agent era                -> agent, certain
                                                               by ownership

Three label sets are built from them, each with a different confound, so that
the spread across sets carries real information about robustness:

  L1 within-era  SIGNED vs UNSIGNED   same repos+era; negatives are impure
                                      (unsigned agent code sits in them), so
                                      any measured separation is a floor
  L2 era         SIGNED vs PRE        clean labels; confounded by era drift
  L3 external    OWN vs PRE           clean labels; confounded by project

Hunks between PRE_ERA and AGENT_ERA are deliberately unused: the transition is
ambiguous and cheap to drop.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np

from authorship import paths
from authorship.features import NAMES

COHORT = paths.COHORT_CORPUS
OWN = paths.OWN_CORPUS
CONTROL = paths.CONTROL_CORPUS

AGENT_ERA = "2024-01-01"   # earliest date treated as agent-capable
PRE_ERA = "2023-01-01"     # latest date treated as certainly pre-agent


def _load(path: Path) -> list[dict]:
    """Read a corpus, preferring the plain file and falling back to the gzipped
    copy that ships in the repository. The committed corpora are gzipped because
    the cohort one is 42 MB of feature vectors uncompressed and 5 MB compressed;
    a gatherer run writes the plain file, which then wins."""
    if path.exists():
        text = path.read_text()
    else:
        packed = path.with_suffix(path.suffix + ".gz")
        if not packed.exists():
            return []
        text = gzip.decompress(packed.read_bytes()).decode()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def load() -> tuple[list[dict], list[dict]]:
    return _load(COHORT), _load(OWN)


def load_control() -> list[dict]:
    """Modern human code from projects that ban AI contributions."""
    return _load(CONTROL)


def populations(cohort: list[dict], own: list[dict],
                control: list[dict] | None = None) -> dict[str, list[dict]]:
    signed = [r for r in cohort if r["agent"] == 1 and r["date"] >= AGENT_ERA]
    unsigned = [r for r in cohort if r["agent"] == 0 and r["date"] >= AGENT_ERA]
    pre = [r for r in cohort if r["date"] and r["date"] < PRE_ERA]
    pops = {"SIGNED": signed, "UNSIGNED": unsigned, "PRE": pre, "OWN": own}
    if control is not None:
        pops["HUMAN_MODERN"] = control
    return pops


def matrix(records: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(features, line weights, repo groups) for a list of records."""
    x = np.array([r["v"] for r in records], dtype=float)
    w = np.array([r["lines"] for r in records], dtype=float)
    g = np.array([r["repo"] for r in records], dtype=object)
    return x, w, g


def label_set(pos: list[dict], neg: list[dict]) -> dict:
    x_p, w_p, g_p = matrix(pos)
    x_n, w_n, g_n = matrix(neg)
    return {
        "x": np.vstack([x_p, x_n]),
        "y": np.concatenate([np.ones(len(pos)), np.zeros(len(neg))]),
        "w": np.concatenate([w_p, w_n]),
        "g": np.concatenate([g_p, g_n]),
        "n_pos": len(pos), "n_neg": len(neg),
        "lines_pos": float(w_p.sum()), "lines_neg": float(w_n.sum()),
    }


LABEL_SETS = {
    "L1_within_era": ("SIGNED", "UNSIGNED"),
    "L2_era": ("SIGNED", "PRE"),
    "L3_external": ("OWN", "PRE"),
}


def build(pops: dict[str, list[dict]]) -> dict[str, dict]:
    return {name: label_set(pops[p], pops[n]) for name, (p, n) in LABEL_SETS.items()
            if pops[p] and pops[n]}


def feature_index(name: str) -> int:
    return NAMES.index(name)
