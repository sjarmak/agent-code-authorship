#!/usr/bin/env python3
"""MODERN-HUMAN control corpus: recent code from projects that ban AI-written
contributions.

The fingerprint's central identification problem is that its clean negative
class (pre-2023 code) is also its OLD class. A classifier can separate
"agent-written" from "written in 2019" by detecting nothing more than modern
formatting. Without modern human code, the estimate is not identified.

These projects supply it. Each one publicly rejects AI/LLM-generated
contributions, so their post-2024 commits are code that the project asserts is
human-written. Policies were adopted at different points from 2024 through 2026
and no policy is self-enforcing, so some agent code has certainly slipped in.
That contamination raises the measured false positive rate, which LOWERS the
resulting agent-share estimate — the error runs in the conservative direction.

Evidence is not taken on faith: each repo is grepped for its own policy text and
recorded in data/control_evidence.json. A repo with no locatable policy language
is dropped from the control set rather than assumed.

Shallow clone (--shallow-since) keeps this affordable; lines whose blame lands on
a shallow boundary commit are discarded, since their real origin is unknown.

Usage: python3 -m authorship.corpus.control [--since 2024-01-01]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from authorship import paths, sg
from authorship.languages import EXT_LANG, ext_of
from authorship.features import vector
from authorship.corpus.own import _clean_env, _git, _path, blame_hunks, code_files, commit_size

OUT = paths.CONTROL_CORPUS
EVIDENCE = paths.CONTROL_EVIDENCE
CLONES = paths.CACHE / "control_clones"
# per-repo shards, so adding one project later does not re-blame the other twenty
SHARDS = paths.CACHE / "control_shards"

# Projects documented as rejecting AI/LLM-generated contributions. Chosen to
# span the languages the COHORT is actually written in — a control set of only
# C projects would let the model learn "C means human". Hosts vary because the
# no-AI crowd is disproportionately off GitHub.
CONTROL_REPOS = (
    "https://github.com/yt-dlp/yt-dlp",                    # Python
    "https://projects.torsion.org/borgmatic-collective/borgmatic",  # Python
    "https://codeberg.org/poezio/poezio",                  # Python
    "https://gitlab.postmarketos.org/postmarketOS/pmbootstrap",     # Python
    "https://github.com/teamtype/teamtype",                # TypeScript?
    "https://github.com/servo/servo",                      # Rust
    "https://github.com/typst/typst",                      # Rust
    "https://github.com/bevyengine/bevy",                  # Rust
    "https://github.com/alacritty/alacritty",              # Rust
    "https://github.com/twpayne/chezmoi",                  # Go
    "https://codeberg.org/superseriousbusiness/gotosocial",  # Go
    "https://github.com/influxdata/telegraf",              # Go
    "https://codeberg.org/wafrn/wafrn",                    # TypeScript
    "https://github.com/pouchdb/pouchdb",                  # JavaScript
    "https://github.com/apache/couchdb",                   # Erlang/JavaScript
    "https://github.com/jhy/jsoup",                        # Java
    "https://github.com/clojure/clojure",                  # Java
    "https://github.com/OpenTTD/OpenTTD",                  # C++
    # Cataclysm-DDA is in the index too but its ~2GB of game-data history makes
    # the shallow clone impractical here; C++ is covered by the two below.
    "https://github.com/endless-sky/endless-sky",          # C++
    "https://github.com/ziglang/zig",                      # C++
    "https://github.com/qemu/qemu",                        # C
    "https://github.com/AsahiLinux/m1n1",                  # C/Python
    "https://github.com/pkgconf/pkgconf",                  # C
)

# Projects put the policy wherever they like — CONTRIBUTING, a CoC, an AGENTS.md
# aimed at the agents themselves, or a docs page. Rather than guess paths, scan
# any tracked file whose NAME looks like policy documentation, at any depth.
POLICY_NAME_RX = re.compile(
    r"(^|/)(contributing|contribute|agents?|ai[-_]?policy|policy|policies|"
    r"code[-_]of[-_]conduct|readme|develop(ing|-on-[a-z0-9-]+)?|hacking|"
    r"code-provenance)\.(md|rst|txt|adoc)$", re.I)
MAX_POLICY_FILES = 40
_AI = (r"\b(AI|LLM|LLMs|large language model[s]?|generative|GenAI|Copilot|"
       r"ChatGPT|machine[- ]generated|AI-generated|AI-assisted)\b")
_NO = (r"\b(must not|may not|not accept|unacceptable|prohibit\w*|forbid\w*|ban|"
       r"banned|reject\w*|disallow\w*|not permitted|refuse\w*|do not (?:use|submit)|"
       r"don'?t (?:use|submit)|not allowed|tainted|will be closed|no-?ai|no-?llm|"
       r"without .{0,30}approval)\b")
# whitespace is normalized before matching, so a policy split across lines still
# reads as one sentence; either order counts (ban-then-AI or AI-then-ban)
POLICY_RX = re.compile(rf"{_AI}.{{0,240}}?{_NO}|{_NO}.{{0,240}}?{_AI}", re.I)

# Some projects keep the policy off-repo and only link to it. Quoting the linked
# page keeps the evidence auditable instead of trusting a third-party index.
EXTERNAL_POLICY: dict[str, dict[str, str]] = {
    "bevyengine/bevy": {
        "file": "https://bevy.org/learn/contribute/policies/ai/ (linked from "
                "CONTRIBUTING.md)",
        "quote": "all forms of AI-generated contributions cannot be merged into "
                 "repositories maintained by the Bevy Organization. This includes "
                 "both code and non-code game assets",
    },
}


def repo_name(url: str) -> str:
    """host-qualified short name, e.g. codeberg.org/wafrn/wafrn -> wafrn/wafrn"""
    return "/".join(url.rstrip("/").removesuffix(".git").split("/")[-2:])


def shallow_clone(url: str, since: str) -> Path | None:
    dest = CLONES / url.rstrip("/").removesuffix(".git").split("/")[-1]
    if (dest / ".git").exists():
        return dest
    CLONES.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--single-branch", f"--shallow-since={since}",
         "--no-tags", url, str(dest)],
        capture_output=True, text=True, check=False,
        env={**_clean_env(), "PATH": _path(), "GIT_TERMINAL_PROMPT": "0"})
    return dest if (dest / ".git").exists() else None


def boundary_shas(repo_dir: Path) -> set[str]:
    f = repo_dir / ".git" / "shallow"
    return set(f.read_text().split()) if f.exists() else set()


def policy_evidence(full_name: str, repo_dir: Path) -> list[dict]:
    """Find the repo's own words rejecting AI contributions."""
    found = []
    tracked = sorted(p for p in _git(repo_dir, "ls-files").splitlines()
                     if POLICY_NAME_RX.search(p))[:MAX_POLICY_FILES]
    for name in tracked:
        try:
            raw = (repo_dir / name).read_text(errors="replace")
        except OSError:
            continue
        text = " ".join(raw.split())
        for m in POLICY_RX.finditer(text):
            found.append({"file": name, "quote": m.group(0)[:240]})
            if len(found) >= 3:
                return found
    if not found and full_name in EXTERNAL_POLICY:
        found.append(EXTERNAL_POLICY[full_name])
    return found


def repo_records(full_name: str, repo_dir: Path, n_files: int, min_lines: int,
                 since: str, bulk_files: int) -> tuple[list[dict], dict]:
    boundary = boundary_shas(repo_dir)
    cache: dict[str, int] = {}
    records, stats = [], {"boundary_dropped": 0, "bulk_dropped": 0}
    for path in sg.stride_sample(code_files(repo_dir), n_files):
        lang = EXT_LANG.get(ext_of(path))
        for sha, date, lines in blame_hunks(repo_dir, path):
            if len(lines) < min_lines or date < since:
                continue
            if sha in boundary:
                stats["boundary_dropped"] += 1
                continue
            if commit_size(repo_dir, sha, cache) > bulk_files:
                stats["bulk_dropped"] += 1
                continue
            records.append({"repo": full_name, "path": path, "lang": lang,
                            "agent": 0, "date": date, "lines": len(lines),
                            "v": [round(x, 4) for x in vector(lines, lang)]})
    return records, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--files", type=int, default=60)
    ap.add_argument("--min-lines", type=int, default=6)
    ap.add_argument("--bulk-files", type=int, default=200)
    ap.add_argument("--refresh", action="store_true",
                    help="re-blame even repos that already have a shard")
    args = ap.parse_args()

    print(f"modern-human control: {len(CONTROL_REPOS)} AI-rejecting projects, "
          f"code committed >= {args.since}\n", flush=True)
    # clone in parallel (network-bound), then blame serially (CPU/disk-bound)
    with ThreadPoolExecutor(max_workers=6) as pool:
        cloned = dict(zip(CONTROL_REPOS,
                          pool.map(lambda u: shallow_clone(u, "2023-06-01"),
                                   CONTROL_REPOS)))

    all_records, evidence, dropped = [], {}, []
    for url in CONTROL_REPOS:
        name = repo_name(url)
        d = cloned.get(url)
        if not d:
            dropped.append(f"{name} (clone failed)")
            print(f"  {name}: clone failed", flush=True)
            continue
        ev = policy_evidence(name, d)
        if not ev:
            dropped.append(f"{name} (no policy text located)")
            print(f"  {name}: no policy text located — DROPPED from control",
                  flush=True)
            continue
        shard = SHARDS / (name.replace("/", "__") + ".jsonl")
        if shard.exists() and not args.refresh:
            recs = [json.loads(ln) for ln in shard.read_text().splitlines() if ln.strip()]
            stats = {"cached": True}
        else:
            recs, stats = repo_records(name, d, args.files, args.min_lines,
                                       args.since, args.bulk_files)
            SHARDS.mkdir(exist_ok=True)
            shard.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n"
                                     for r in recs))
        evidence[name] = {"policy": ev, "hunks": len(recs),
                          "lines": sum(r["lines"] for r in recs), **stats}
        all_records.extend(recs)
        print(f"  {name}: {len(recs)} hunks, {sum(r['lines'] for r in recs):,} lines "
              f"| policy in {ev[0]['file']}", flush=True)

    OUT.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n"
                           for r in all_records))
    EVIDENCE.write_text(json.dumps({"since": args.since, "repos": evidence,
                                    "dropped": dropped}, indent=2) + "\n")
    print(f"\n  {len(all_records):,} hunks, "
          f"{sum(r['lines'] for r in all_records):,} lines -> {OUT.name}")
    if dropped:
        print("  dropped: " + "; ".join(dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
