#!/usr/bin/env python3
"""Gather a labeled code corpus for the agent-written fingerprint.

For every repo in the expanded cohort: sample files at HEAD, pull the blob and
its blame in ONE query, then split the file into blame hunks. Each hunk of at
least --min-lines becomes a record carrying

    repo, path, lang, agent (trailer-signed?), date (introducing commit),
    lines, v (features.py vector)

Three label sources come out of the same pass and are separated downstream:
  agent=1 and date>=2024   trailer-signed agent code (clean positives)
  agent=0 and date<2023    pre-agent-era code (clean negatives, by era)
  agent=0 and date>=2024   UNLABELED — the population whose agent share we
                           want to estimate (contains unsigned agent code)

Resumable: appends to corpora/cohort.jsonl and skips repos already listed in
corpora/cohort.done. Env: source ~/.env; SRC_ENDPOINT=S2.

Usage: python3 -m authorship.corpus.cohort [--files 50] [--workers 8] [--repos N]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from authorship import paths, sg
from authorship.trailers import match_agents
from authorship.languages import EXT_LANG, ext_of
from authorship.sg import resolve
from authorship.features import vector

OUT = paths.COHORT_CORPUS
DONE = paths.CORPORA / "cohort.done"
EXTRA_REPOS = ("gastownhall/gascity",)

BLOB_AND_BLAME = (
    'query($repo:String!,$path:String!){repository(name:$repo)'
    '{commit(rev:"HEAD"){blob(path:$path){content '
    'blame(startLine:1,endLine:5000){startLine endLine commit{author{date} message}}}}}}')

_write_lock = threading.Lock()


def cohort_repos() -> list[str]:
    ages = json.loads(paths.COHORT_AGES.read_text())
    names = [r["full_name"] for r in ages]
    return names + [r for r in EXTRA_REPOS if r not in names]


def fork_map() -> dict[str, str]:
    return sg.fork_map()


def file_records(repo: str, orig: str, path: str, min_lines: int) -> list[dict]:
    """One query -> the hunk records for one file."""
    lang = EXT_LANG.get(ext_of(path))
    if not lang:
        return []
    blob = (((sg.api(BLOB_AND_BLAME, repo=repo, path=path)
              .get("repository") or {}).get("commit") or {}).get("blob") or {})
    content = blob.get("content")
    if not content:
        return []
    lines = content.split("\n")
    out = []
    for h in (blob.get("blame") or []):
        start, end = h.get("startLine", 0), h.get("endLine", 0)
        if end - start + 1 < min_lines or start < 1:
            continue
        block = lines[start - 1:end]
        if not block:
            continue
        commit = h.get("commit") or {}
        date = ((commit.get("author") or {}).get("date") or "")[:10]
        out.append({
            "repo": orig, "path": path, "lang": lang,
            "agent": 1 if match_agents(commit.get("message", "")) else 0,
            "date": date, "lines": len(block),
            "v": [round(x, 4) for x in vector(block, lang)],
        })
    return out


def gather_repo(orig: str, repo: str, n_files: int, min_lines: int) -> tuple[str, int, int]:
    files = sg.stride_sample(sg.code_files(repo), n_files)
    records, failed = [], 0
    for path in files:
        try:
            records.extend(file_records(repo, orig, path, min_lines))
        except Exception:
            failed += 1
    if records:
        payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        with _write_lock:
            with OUT.open("a") as fh:
                fh.write(payload)
            with DONE.open("a") as fh:
                fh.write(orig + "\n")
    return orig, len(records), failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", type=int, default=50, help="files sampled per repo")
    ap.add_argument("--min-lines", type=int, default=6, help="smallest hunk kept")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--repos", type=int, default=0, help="cap repos (0 = all)")
    args = ap.parse_args()

    sg.check_auth()
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    todo = [r for r in cohort_repos() if r not in done]
    if args.repos:
        todo = todo[:args.repos]
    fmap = fork_map()
    print(f"gather: {len(todo)} repos to do ({len(done)} already done), "
          f"{args.files} files each, {args.workers} workers\n", flush=True)

    resolved, unindexed = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(resolve, o, fmap): o for o in todo}
        for f in as_completed(futs):
            orig = futs[f]
            try:
                repo = f.result()
            except Exception:
                repo = None
            (resolved.append((orig, repo)) if repo else unindexed.append(orig))
    print(f"  resolved {len(resolved)}, unindexed {len(unindexed)}", flush=True)

    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(gather_repo, o, r, args.files, args.min_lines): o
                for o, r in resolved}
        for i, f in enumerate(as_completed(futs), 1):
            orig = futs[f]
            try:
                _, n, failed = f.result()
            except Exception as e:
                print(f"  [{i}/{len(futs)}] {orig}: FAILED ({e})", flush=True)
                continue
            total += n
            print(f"  [{i}/{len(futs)}] {orig}: {n} hunks ({failed} file errors)", flush=True)

    print(f"\n  {total:,} hunk records appended -> {OUT.name}")
    if unindexed:
        print(f"  unindexed (skipped): {', '.join(unindexed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
