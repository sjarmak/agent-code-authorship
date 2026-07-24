"""Where things live. Every path in this package resolves through here, so the
repository can be cloned anywhere and nothing points back at the machine it was
written on.

Layout:
    data/       inputs that are part of the repository (cohort manifest, the
                control group's policy evidence)
    corpora/    extracted feature records, one JSON object per hunk per line
    results/    fitted coefficients, metrics and estimates
    .cache/     clones and other scratch, never committed. Override with
                AUTHORSHIP_CACHE to keep multi-gigabyte clones off your SSD's
                main volume.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CORPORA = ROOT / "corpora"
RESULTS = ROOT / "results"
CACHE = Path(os.environ.get("AUTHORSHIP_CACHE", ROOT / ".cache"))

COHORT_CORPUS = CORPORA / "cohort.jsonl"
OWN_CORPUS = CORPORA / "own.jsonl"
CONTROL_CORPUS = CORPORA / "control.jsonl"

COHORT_FORKS = DATA / "cohort_forks.tsv"
COHORT_AGES = DATA / "cohort_ages.json"
CONTROL_EVIDENCE = DATA / "control_evidence.json"


def cache_dir(name: str) -> Path:
    d = CACHE / name
    d.mkdir(parents=True, exist_ok=True)
    return d
