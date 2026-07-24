#!/usr/bin/env python3
"""Ground-truth AGENT-POSITIVE corpus from repositories one person vouches for.

The trailer method can only see agent code whose committer left a trailer on.
Breaking out of that needs positives that are trusted for a different reason, so
this reads the repositories of an owner who can state from first-hand knowledge
that everything committed there in the agent era was agent-written. It is the one
label in the project that rests on testimony rather than measurement, which is
why the guards below matter and why the estimate is also computed with
trailer-signed positives instead (see identify.py).

Records use the same schema as corpus/cohort.py (repo/path/lang/agent/date/lines/v)
so both corpora feed one model. Two filters keep the label honest:

  bulk-import guard  a hunk is dropped if its introducing commit touched more
                     than --bulk-files files, which is what vendoring, "stripped"
                     copies of other projects and data dumps look like.
  era guard          only hunks from commits on/after --since count as agent.

Repositories that cannot carry the label — vendored copies, benchmark corpora of
other people's code, data archives — are named in EXCLUDE with the reason rather
than silently dropped, because a reader has to be able to check that judgement.
EXCLUDE ships with the entries for the owner this was first run against; pass a
different --owner and you must review it yourself.

Usage: python3 -m authorship.corpus.own --owner <github-user> [--since 2025-01-01]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from authorship import paths, sg
from authorship.languages import EXT_LANG, ext_of
from authorship.features import vector

OUT = paths.OWN_CORPUS
CLONES = paths.CACHE / "own_clones"

# repo -> why it cannot serve as agent-authored ground truth
EXCLUDE = {
    "kubernetes-stripped": "vendored copy of kubernetes/kubernetes",
    "CodeScaleBench": "benchmark corpus of third-party C++ code",
    "CodeContextBench_Dashboard": "benchmark corpus + vendored task repos",
    "EnterpriseBench": "benchmark corpus of third-party code",
    "IR-SDLC-Factory": "adaptation of upstream SWE-Factory",
    "ccb-big-code-mcp": "mined third-party benchmark tasks",
    "ccb-github-mined": "mined third-party PRs (PyTorch)",
    "ccb-kubernetes-docs": "mined third-party benchmark tasks",
    "ccb-10figure": "mined third-party corpus",
    "gas-city-jsonl-archive": "data archive, not code",
    "website": "asset-heavy site export",
    "save_ADS_SciX": "derived from upstream ADS/SciX UI",
    "quepid-notebooks": "upstream Quepid notebooks",
    "scix-solr-proxy": "upstream proxy config",
    "Mars_RSL_LIGGGHTS": "LIGGGHTS input scripts, pre-agent scientific work",
    "mem-beads": "issue mirror, not code",
    "codeprobe-beads": "issue mirror, not code",
    "gas-city-beads": "issue mirror, not code",
    "homebrew-tap": "generated formula",
}
MAX_DISK_KB = 60_000  # clone-time cap; larger repos are listed as skipped


def repo_list(owner: str, since: str) -> tuple[list[dict], list[str]]:
    out = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "300", "--json",
         "nameWithOwner,createdAt,isFork,isArchived,diskUsage,primaryLanguage"],
        capture_output=True, text=True, check=True,
        env={**_clean_env(), "PATH": _path()})
    repos, skipped = [], []
    for r in json.loads(out.stdout):
        name = r["nameWithOwner"].split("/")[-1]
        if r["isFork"] or r["createdAt"][:10] < since:
            continue
        if name in EXCLUDE:
            skipped.append(f"{name} ({EXCLUDE[name]})")
        elif r["diskUsage"] > MAX_DISK_KB:
            skipped.append(f"{name} (>{MAX_DISK_KB // 1000}MB clone)")
        else:
            repos.append(r)
    return repos, skipped


def _clean_env() -> dict:
    import os
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")


def _git(repo_dir: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo_dir), *args],
                         capture_output=True, text=True, check=False)
    return res.stdout if res.returncode == 0 else ""


def clone(full_name: str) -> Path | None:
    dest = CLONES / full_name.split("/")[-1]
    if (dest / ".git").exists():
        return dest
    CLONES.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(["git", "clone", "--quiet", f"https://github.com/{full_name}.git",
                          str(dest)], capture_output=True, text=True, check=False,
                         env={**_clean_env(), "PATH": _path()})
    return dest if res.returncode == 0 else None


def code_files(repo_dir: Path) -> list[str]:
    names = _git(repo_dir, "ls-files").splitlines()
    return [p for p in names
            if EXT_LANG.get(ext_of(p)) and not any(s in p for s in SKIP_FRAGMENTS)]


def commit_size(repo_dir: Path, sha: str, cache: dict[str, int]) -> int:
    if sha not in cache:
        out = _git(repo_dir, "show", "--pretty=format:", "--name-only", sha)
        cache[sha] = len([ln for ln in out.splitlines() if ln.strip()])
    return cache[sha]


def blame_hunks(repo_dir: Path, path: str):
    """Yield (sha, date, lines) for contiguous same-commit runs at HEAD."""
    out = _git(repo_dir, "blame", "--porcelain", "--", path)
    if not out:
        return
    cur_sha, cur_date, run = None, "", []
    dates: dict[str, str] = {}
    for ln in out.split("\n"):
        if ln.startswith("\t"):
            run.append(ln[1:])
            continue
        head = ln.split(" ")
        if len(head[0]) == 40 and len(head) >= 3:
            sha = head[0]
            if sha != cur_sha:
                if cur_sha and run:
                    yield cur_sha, cur_date, run
                cur_sha, run = sha, []
            cur_date = dates.get(sha, cur_date)
        elif ln.startswith("author-time ") and cur_sha:
            from datetime import datetime, timezone
            dates[cur_sha] = datetime.fromtimestamp(
                int(ln.split()[1]), tz=timezone.utc).date().isoformat()
            cur_date = dates[cur_sha]
    if cur_sha and run:
        yield cur_sha, cur_date, run


def repo_records(full_name: str, repo_dir: Path, n_files: int, min_lines: int,
                 since: str, bulk_files: int) -> tuple[list[dict], int]:
    cache: dict[str, int] = {}
    records, dropped_bulk = [], 0
    for path in sg.stride_sample(code_files(repo_dir), n_files):
        lang = EXT_LANG.get(ext_of(path))
        for sha, date, lines in blame_hunks(repo_dir, path):
            if len(lines) < min_lines or date < since:
                continue
            if commit_size(repo_dir, sha, cache) > bulk_files:
                dropped_bulk += 1
                continue
            records.append({"repo": full_name, "path": path, "lang": lang,
                            "agent": 1, "date": date, "lines": len(lines),
                            "v": [round(x, 4) for x in vector(lines, lang)]})
    return records, dropped_bulk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="sjarmak",
                    help="GitHub owner whose repositories carry the vouched label")
    ap.add_argument("--since", default="2025-01-01", help="agent-era cutoff")
    ap.add_argument("--files", type=int, default=40)
    ap.add_argument("--min-lines", type=int, default=6)
    ap.add_argument("--bulk-files", type=int, default=200,
                    help="drop hunks from commits touching more files than this")
    args = ap.parse_args()

    repos, skipped = repo_list(args.owner, args.since)
    print(f"{args.owner} ground truth: {len(repos)} repos created >= {args.since}, "
          f"{len(skipped)} excluded\n", flush=True)
    all_records, bulk_total = [], 0
    for r in repos:
        full = r["nameWithOwner"]
        d = clone(full)
        if not d:
            print(f"  {full}: clone failed", flush=True)
            continue
        recs, bulk = repo_records(full, d, args.files, args.min_lines,
                                  args.since, args.bulk_files)
        bulk_total += bulk
        all_records.extend(recs)
        print(f"  {full}: {len(recs)} hunks, {sum(x['lines'] for x in recs):,} lines"
              f"{f' ({bulk} bulk-import hunks dropped)' if bulk else ''}", flush=True)

    OUT.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in all_records))
    print(f"\n  {len(all_records):,} hunks, {sum(r['lines'] for r in all_records):,} lines "
          f"-> {OUT.name}  ({bulk_total} dropped by bulk-import guard)")
    print("  excluded: " + "; ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
