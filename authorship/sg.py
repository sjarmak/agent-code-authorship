"""Sourcegraph access: the one place that talks to an instance.

Blame is the reason this project needs Sourcegraph at all. Attributing a
surviving line to its introducing commit requires walking history, which the
GitHub API will not do for you, and cloning 150 repositories to run git blame
locally is an afternoon of disk. A Sourcegraph instance that already indexes the
repositories answers it in one query per file.

Queries go through the `src` CLI so authentication is whatever `src login`
already established:

    export SRC_ENDPOINT=https://sourcegraph.example.com
    export SRC_ACCESS_TOKEN=...

Repositories the instance does not index can be forked into an organization it
does index; `resolve()` prefers the directly-indexed original and falls back to
the fork named in the cohort manifest.
"""

from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import urlparse

from authorship.languages import SKIP_PATH, is_code_path
from authorship.paths import COHORT_FORKS

SOURCEGRAPH_API_TIMEOUT_SECONDS = 90


def _src_environment() -> dict[str, str]:
    """Accept both src-cli and connected Sourcegraph MCP environment names."""
    environment = dict(os.environ)
    if "SRC_ENDPOINT" not in environment and environment.get("SOURCEGRAPH_MCP_URL"):
        parsed = urlparse(environment["SOURCEGRAPH_MCP_URL"])
        environment["SRC_ENDPOINT"] = f"{parsed.scheme}://{parsed.netloc}"
    if "SRC_ACCESS_TOKEN" not in environment and environment.get(
        "SOURCEGRAPH_ACCESS_TOKEN"
    ):
        environment["SRC_ACCESS_TOKEN"] = environment["SOURCEGRAPH_ACCESS_TOKEN"]
    return environment


def _variable_arguments(variables: dict[str, object]) -> list[str]:
    if all(isinstance(value, str) for value in variables.values()):
        return [f"{key}={value}" for key, value in variables.items()]
    payload = json.dumps(variables, separators=(",", ":"))
    return ["-vars", payload]


def api(graphql_query: str, **variables: object) -> dict:
    """Run one GraphQL query. String variables only — ints are inlined by callers,
    because src-cli is particular about variable typing."""
    cmd = ["src", "api", "-query", graphql_query]
    cmd += _variable_arguments(variables)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_src_environment(),
            timeout=SOURCEGRAPH_API_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"src api timed out after {SOURCEGRAPH_API_TIMEOUT_SECONDS} seconds"
        ) from error
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"src api failed: {(result.stderr or result.stdout)[:300]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"unparseable src api output: {error}") from error
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'])[:300]}")
    return payload.get("data", {})


def check_auth() -> str:
    user = (api("{ currentUser { username } }").get("currentUser") or {}).get(
        "username"
    )
    if not user:
        raise SystemExit(
            "src is not authenticated. Set SRC_ENDPOINT and "
            "SRC_ACCESS_TOKEN, then run `src login $SRC_ENDPOINT`."
        )
    return user


def cloned(repo: str) -> bool:
    query = "query($repo:String!){repository(name:$repo){mirrorInfo{cloned}}}"
    try:
        repository = api(query, repo=repo).get("repository") or {}
        return bool((repository.get("mirrorInfo") or {}).get("cloned"))
    except RuntimeError:
        return False


def fork_map() -> dict[str, str]:
    """original 'owner/name' -> the indexed fork standing in for it."""
    pairs = [
        line.split("\t")
        for line in COHORT_FORKS.read_text().splitlines()
        if line.strip()
    ]
    return {original: indexed for indexed, original in pairs}


def resolve(original: str, forks: dict[str, str] | None = None) -> str | None:
    """An indexed, cloned repository name for this original, or None."""
    forks = fork_map() if forks is None else forks
    direct = "github.com/" + original
    if cloned(direct):
        return direct
    fork = forks.get(original)
    return fork if fork and cloned(fork) else None


def code_files(repo: str) -> list[str]:
    """Source paths at HEAD, skipping vendored and generated trees."""
    query = (
        'query($repo:String!){repository(name:$repo){commit(rev:"HEAD")'
        '{tree(path:""){files(recursive:true){path}}}}}'
    )
    data = api(query, repo=repo)
    files = (
        ((data.get("repository") or {}).get("commit") or {}).get("tree") or {}
    ).get("files") or []
    return [
        f["path"]
        for f in files
        if is_code_path(f.get("path", "")) and not SKIP_PATH(f.get("path", ""))
    ]


def stride_sample(items: list[str], k: int) -> list[str]:
    """Deterministic stride sample. No RNG, so a rerun gathers the same files and
    a changed number is a changed corpus rather than a reshuffled one."""
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


BLOB_AND_BLAME = (
    "query($repo:String!,$path:String!){repository(name:$repo)"
    '{commit(rev:"HEAD"){blob(path:$path){content '
    "blame(startLine:1,endLine:5000){startLine endLine commit{author{date} message}}}}}}"
)


def blob_and_blame(repo: str, path: str) -> dict:
    """File content and its blame in one round trip. Returns {} when the path is
    missing or empty at HEAD."""
    return (
        (api(BLOB_AND_BLAME, repo=repo, path=path).get("repository") or {}).get(
            "commit"
        )
        or {}
    ).get("blob") or {}
