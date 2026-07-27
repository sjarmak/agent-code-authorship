"""Pinned, provenance-preserving local-Git corpus extraction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from authorship.features import vector
from authorship.language_features import (
    PRIMARY_SCHEMA_VERSION,
    code_kind,
    primary_vector,
)
from authorship.languages import SKIP_FRAGMENTS, lang_of
from authorship.manifest import load_manifest


REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
FEATURE_SCHEMA = {
    "baseline": 1,
    "primary": PRIMARY_SCHEMA_VERSION,
    "corpus_selection": 2,
}


class CorpusError(RuntimeError):
    """Raised when provenance or archive verification fails."""


def _clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ("GH_TOKEN", "GITHUB_TOKEN")
    }


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve()}", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        env=_clean_env(),
    )
    if check and result.returncode:
        raise CorpusError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def repository_cache_path(cache_root: Path, repo_id: str) -> Path:
    if not REPO_ID.fullmatch(repo_id):
        raise CorpusError(f"unsafe repository id: {repo_id!r}")
    return cache_root / repo_id.replace("/", "__")


def clone_pinned(entry: dict[str, Any], cache_root: Path) -> Path:
    url = entry["url"]
    if not url.startswith("https://"):
        raise CorpusError("repository URL must use https")
    destination = repository_cache_path(cache_root, entry["id"])
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_resolved = cache_root.resolve()
    if destination.is_symlink() or not destination.resolve().is_relative_to(cache_resolved):
        raise CorpusError("unsafe cache destination")
    if not (destination / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                f"{url.rstrip('/')}.git",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**_clean_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode:
            raise CorpusError(result.stderr.strip() or f"clone failed for {entry['id']}")
    commit = entry["snapshot"]["commit"]
    if _git(
        destination, "rev-parse", "--verify", f"{commit}^{{commit}}", check=False
    ) != commit:
        _git(destination, "fetch", "--quiet", "origin", commit)
    verify_snapshot(destination, entry)
    return destination


def load_corpus_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    roles = {entry.get("role") for entry in document.get("repositories", [])}
    if roles != {"target"}:
        return load_manifest(path)
    required = {
        "id",
        "url",
        "role",
        "label",
        "languages",
        "snapshot",
        "effective_date_range",
        "excluded_paths",
        "content_checksum",
    }
    for entry in document["repositories"]:
        missing = required - set(entry)
        if missing:
            raise CorpusError(
                f"{entry.get('id', '<unknown>')} target missing {sorted(missing)}"
            )
        if entry["label"] != "unlabeled":
            raise CorpusError("target labels must remain unknown")
    return document


def verify_snapshot(repo: Path, entry: dict[str, Any]) -> None:
    commit = entry["snapshot"]["commit"]
    resolved = _git(repo, "rev-parse", f"{commit}^{{commit}}")
    if resolved != commit:
        raise CorpusError(f"{entry['id']} commit mismatch")
    tree = _git(repo, "show", "-s", "--format=%T", commit)
    if tree != entry["snapshot"]["tree"]:
        raise CorpusError(f"{entry['id']} tree mismatch: {tree}")


def _eligible_paths(repo: Path, entry: dict[str, Any]) -> list[tuple[str, str]]:
    commit = entry["snapshot"]["commit"]
    paths = _git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    allowed = set(entry["languages"])
    excluded = tuple(SKIP_FRAGMENTS) + tuple(entry["excluded_paths"])
    return [
        (path, language)
        for path in paths
        if (language := lang_of(path)) in allowed
        and not any(fragment.lower() in path.lower() for fragment in excluded)
    ]


def _blame_lines(repo: Path, commit: str, path: str) -> list[dict[str, Any]]:
    try:
        output = _git(repo, "blame", "--line-porcelain", commit, "--", path)
    except UnicodeDecodeError:
        return []
    lines: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in output.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$", raw)
        if header:
            current = {"commit": header.group(1), "line": int(header.group(2))}
        elif current is not None and raw.startswith("author-time "):
            current["timestamp"] = int(raw.split()[1])
        elif current is not None and raw.startswith("\t"):
            current["text"] = raw[1:]
            if "timestamp" not in current:
                raise CorpusError(f"blame lacks author time for {path}")
            lines.append(current)
            current = None
    return lines


def _hunks(lines: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    run: list[dict[str, Any]] = []
    for line in lines:
        if run and (
            line["commit"] != run[-1]["commit"]
            or line["line"] != run[-1]["line"] + 1
        ):
            yield run
            run = []
        run.append(line)
    if run:
        yield run


def _range(entry: dict[str, Any]) -> tuple[datetime | None, datetime]:
    start, end = entry["effective_date_range"]
    lower = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    upper = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return lower, upper


def extract_repository(
    repo: Path,
    entry: dict[str, Any],
    min_lines: int = 6,
    max_files: int | None = None,
    workers: int = 1,
) -> list[dict[str, Any]]:
    verify_snapshot(repo, entry)
    lower, upper = _range(entry)
    records: list[dict[str, Any]] = []
    paths = sorted(_eligible_paths(repo, entry))
    if max_files is not None:
        if max_files < 1:
            raise CorpusError("max_files must be positive")
        paths = paths[:max_files]
    if workers < 1:
        raise CorpusError("workers must be positive")

    def extract_path(item: tuple[str, str]) -> list[dict[str, Any]]:
        path, language = item
        path_records = []
        for hunk in _hunks(_blame_lines(repo, entry["snapshot"]["commit"], path)):
            introduced = datetime.fromtimestamp(hunk[0]["timestamp"], tz=timezone.utc)
            if len(hunk) < min_lines or (lower and introduced < lower) or introduced > upper:
                continue
            source_lines = [line["text"] for line in hunk]
            canonical = ("\n".join(source_lines) + "\n").encode()
            baseline = [round(value, 6) for value in vector(source_lines, language)]
            primary = [
                round(value, 6) for value in primary_vector(source_lines, language)
            ]
            path_records.append(
                {
                    "repo": entry["id"],
                    "role": entry["role"],
                    "label": entry["label"],
                    "snapshot_commit": entry["snapshot"]["commit"],
                    "path": path,
                    "lang": language,
                    "introducing_commit": hunk[0]["commit"],
                    "introduced_at": introduced.date().isoformat(),
                    "start_line": hunk[0]["line"],
                    "end_line": hunk[-1]["line"],
                    "line_count": len(hunk),
                    "content_sha256": hashlib.sha256(canonical).hexdigest(),
                    "code_kind": code_kind(path),
                    "feature_schema": FEATURE_SCHEMA,
                    "v_baseline": baseline,
                    "v_primary": primary,
                    "v": primary,
                }
            )
        return path_records

    if workers == 1:
        parts = map(extract_path, paths)
    else:
        executor = ThreadPoolExecutor(max_workers=workers)
        parts = executor.map(extract_path, paths)
    try:
        for part in parts:
            records.extend(part)
    finally:
        if workers != 1:
            executor.shutdown(wait=True, cancel_futures=True)
    return records


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _meta_path(shard: Path) -> Path:
    return shard.with_suffix(shard.suffix + ".meta.json")


def write_completed_shard(
    shard: Path, records: list[dict[str, Any]], entry: dict[str, Any]
) -> None:
    shard.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode()
    with tempfile.NamedTemporaryFile(dir=shard.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, shard)
    meta = {
        "repo": entry["id"],
        "snapshot_commit": entry["snapshot"]["commit"],
        "feature_schema": FEATURE_SCHEMA,
        "record_count": len(records),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    meta_path = _meta_path(shard)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=meta_path.parent, delete=False
    ) as stream:
        meta_temporary = Path(stream.name)
        json.dump(meta, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(meta_temporary, meta_path)


def load_completed_shard(
    shard: Path, entry: dict[str, Any]
) -> list[dict[str, Any]] | None:
    meta_path = _meta_path(shard)
    if not shard.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        payload = shard.read_bytes()
        if (
            meta["repo"] != entry["id"]
            or meta["snapshot_commit"] != entry["snapshot"]["commit"]
            or meta.get("feature_schema") != FEATURE_SCHEMA
            or meta["sha256"] != hashlib.sha256(payload).hexdigest()
        ):
            return None
        records = [json.loads(line) for line in payload.splitlines() if line]
        return records if len(records) == meta["record_count"] else None
    except (KeyError, json.JSONDecodeError, OSError):
        return None


def archive_repository(
    repo: Path,
    archive_root: Path,
    entry: dict[str, Any],
    *,
    allowed_cache_root: Path,
    remove_local: bool = False,
) -> dict[str, str]:
    repo_resolved = repo.resolve()
    cache_resolved = allowed_cache_root.resolve()
    archive_resolved = archive_root.resolve()
    if (
        repo.is_symlink()
        or repo_resolved == cache_resolved
        or not repo_resolved.is_relative_to(cache_resolved)
    ):
        raise CorpusError("repository is outside the allowed cache root")
    if archive_resolved == repo_resolved or archive_resolved.is_relative_to(repo_resolved):
        raise CorpusError("archive root must be outside the repository")
    verify_snapshot(repo, entry)
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"{entry['id'].replace('/', '__')}.bundle"
    with tempfile.NamedTemporaryFile(
        dir=archive_root, suffix=".bundle", delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        _git(repo, "bundle", "create", str(temporary), "--all")
        _git(repo, "bundle", "verify", str(temporary))
        before = _sha256_file(temporary)
        os.replace(temporary, destination)
        after = _sha256_file(destination)
        if before != after:
            raise CorpusError("archive checksum changed after placement")
        if remove_local:
            shutil.rmtree(repo_resolved)
        return {"archive": str(destination), "sha256": after}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("study/repositories.v1.json"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--min-lines", type=int, default=6)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--max-files",
        type=int,
        help="deterministic representative subset; omit for the full gather",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path(os.environ["AUTHORSHIP_ARCHIVE"])
        if os.environ.get("AUTHORSHIP_ARCHIVE")
        else None,
        help="verified archive tier; completed clones are offloaded automatically",
    )
    args = parser.parse_args()
    manifest = load_corpus_manifest(args.manifest)
    selected = set(args.repo)
    for entry in manifest["repositories"]:
        if selected and entry["id"] not in selected:
            continue
        shard = args.shards / f"{entry['id'].replace('/', '__')}.jsonl"
        records = load_completed_shard(shard, entry)
        if records is None:
            repo = clone_pinned(entry, args.cache)
            records = extract_repository(
                repo,
                entry,
                args.min_lines,
                max_files=args.max_files,
                workers=args.workers,
            )
            write_completed_shard(shard, records, entry)
            if args.archive:
                archive_repository(
                    repo,
                    args.archive,
                    entry,
                    allowed_cache_root=args.cache,
                    remove_local=True,
                )
        print(f"{entry['id']}: {len(records)} hunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
