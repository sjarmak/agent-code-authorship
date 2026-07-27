import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO

from authorship.corpus.pinned import (
    CorpusError,
    archive_repository,
    clone_pinned,
    extract_repository,
    load_completed_shard,
    load_corpus_manifest,
    repository_cache_path,
    main,
    verify_snapshot,
    write_completed_shard,
)


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, date: str) -> str:
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    git(repo, "add", ".")
    git(repo, "commit", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD")


class PinnedCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "cache" / "owner__project"
        self.repo.mkdir(parents=True)
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")

        (self.repo / "module.py").write_text(
            "\n".join(f"old_{i} = {i}" for i in range(6)) + "\n"
        )
        commit(self.repo, "old", "2023-01-02T00:00:00Z")
        with (self.repo / "module.py").open("a") as stream:
            stream.write("\n".join(f"modern_{i} = {i}" for i in range(6)) + "\n")
        (self.repo / "generated.py").write_text("value = 1\n" * 8)
        self.snapshot = commit(self.repo, "modern", "2025-05-06T00:00:00Z")
        self.tree = git(self.repo, "show", "-s", "--format=%T", self.snapshot)
        self.entry = {
            "id": "owner/project",
            "url": "https://example.invalid/owner/project",
            "role": "reference",
            "label": "agent",
            "languages": ["Python"],
            "snapshot": {
                "commit": self.snapshot,
                "committed_at": "2025-05-06T00:00:00Z",
                "tree": self.tree,
            },
            "evidence": {
                "tier": 1,
                "kind": "maintainer_attestation",
                "scope": "entire_repository_history",
            },
            "effective_date_range": ["2024-01-01T00:00:00Z", "2026-07-24T23:59:59Z"],
            "excluded_paths": ["generated"],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_path_rejects_traversal(self):
        cache = self.root / "cache"

        self.assertEqual(
            repository_cache_path(cache, "owner/project"),
            cache / "owner__project",
        )
        with self.assertRaises(CorpusError):
            repository_cache_path(cache, "../../escape")

    def test_clone_rejects_non_https_url(self):
        entry = json.loads(json.dumps(self.entry))
        entry["url"] = "file:///tmp/attacker-controlled"

        with self.assertRaisesRegex(CorpusError, "https"):
            clone_pinned(entry, self.root / "cache")

    def test_clone_rejects_symlinked_cache_destination(self):
        cache = self.root / "new-cache"
        cache.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (cache / "owner__project").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(CorpusError, "cache destination"):
            clone_pinned(self.entry, cache)

    def test_snapshot_tree_mismatch_fails_closed(self):
        verify_snapshot(self.repo, self.entry)
        broken = json.loads(json.dumps(self.entry))
        broken["snapshot"]["tree"] = "0" * 40

        with self.assertRaisesRegex(CorpusError, "tree mismatch"):
            verify_snapshot(self.repo, broken)

    def test_extract_filters_old_and_generated_code(self):
        records = extract_repository(self.repo, self.entry, min_lines=3)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["path"], "module.py")
        self.assertEqual(record["introduced_at"], "2025-05-06")
        self.assertEqual(record["start_line"], 7)
        self.assertEqual(record["end_line"], 12)
        self.assertEqual(record["line_count"], 6)
        self.assertNotIn("source", record)
        self.assertEqual(
            record["feature_schema"],
            {"baseline": 1, "primary": 1, "corpus_selection": 2},
        )
        self.assertEqual(record["code_kind"], "production")
        self.assertEqual(len(record["v_baseline"]), len(record["v_primary"]))
        self.assertEqual(record["v"], record["v_primary"])
        expected = "\n".join(f"modern_{i} = {i}" for i in range(6)) + "\n"
        self.assertEqual(
            record["content_sha256"],
            hashlib.sha256(expected.encode()).hexdigest(),
        )

    def test_fixture_directory_is_excluded(self):
        fixture = self.repo / "tests" / "fixtures" / "sample.py"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("value = 1\n" * 8)
        snapshot = commit(self.repo, "fixture", "2025-05-07T00:00:00Z")
        self.entry["snapshot"] = {
            "commit": snapshot,
            "committed_at": "2025-05-07T00:00:00Z",
            "tree": git(self.repo, "show", "-s", "--format=%T", snapshot),
        }

        records = extract_repository(self.repo, self.entry, min_lines=3)

        self.assertNotIn("tests/fixtures/sample.py", [row["path"] for row in records])

    def test_non_utf8_source_is_excluded_without_fabricated_tokens(self):
        line = b"# coding: latin-1\nname = '" + bytes([0xB1]) + b"'\n"
        (self.repo / "legacy.py").write_bytes(line * 4)
        snapshot = commit(self.repo, "legacy encoding", "2025-05-07T00:00:00Z")
        self.entry["snapshot"] = {
            "commit": snapshot,
            "committed_at": "2025-05-07T00:00:00Z",
            "tree": git(self.repo, "show", "-s", "--format=%T", snapshot),
        }

        records = extract_repository(self.repo, self.entry, min_lines=3)

        self.assertNotIn("legacy.py", [row["path"] for row in records])

    def test_representative_file_limit_is_deterministic(self):
        (self.repo / "second.py").write_text("x = 1\n" * 6)
        self.snapshot = commit(self.repo, "second", "2025-05-07T00:00:00Z")
        self.entry["snapshot"] = {
            "commit": self.snapshot,
            "committed_at": "2025-05-07T00:00:00Z",
            "tree": git(self.repo, "show", "-s", "--format=%T", self.snapshot),
        }

        records = extract_repository(self.repo, self.entry, min_lines=3, max_files=1)

        self.assertEqual({record["path"] for record in records}, {"module.py"})

    def test_parallel_extraction_preserves_serial_order_and_content(self):
        (self.repo / "second.py").write_text("x = 1\n" * 6)
        snapshot = commit(self.repo, "parallel", "2025-05-07T00:00:00Z")
        self.entry["snapshot"] = {
            "commit": snapshot,
            "committed_at": "2025-05-07T00:00:00Z",
            "tree": git(self.repo, "show", "-s", "--format=%T", snapshot),
        }

        serial = extract_repository(self.repo, self.entry, min_lines=3)
        parallel = extract_repository(self.repo, self.entry, min_lines=3, workers=3)

        self.assertEqual(parallel, serial)

    def test_completed_shard_is_resumable_and_tamper_evident(self):
        shard = self.root / "shards" / "owner__project.jsonl"
        records = extract_repository(self.repo, self.entry, min_lines=3)
        write_completed_shard(shard, records, self.entry)

        self.assertEqual(load_completed_shard(shard, self.entry), records)
        with shard.open("a") as stream:
            stream.write("{}\n")
        self.assertIsNone(load_completed_shard(shard, self.entry))

    def test_completed_shard_is_invalidated_by_feature_schema_change(self):
        shard = self.root / "shards" / "owner__project.jsonl"
        records = extract_repository(self.repo, self.entry, min_lines=3)
        write_completed_shard(shard, records, self.entry)
        meta_path = shard.with_suffix(".jsonl.meta.json")
        meta = json.loads(meta_path.read_text())
        meta["feature_schema"]["primary"] = 0
        meta_path.write_text(json.dumps(meta))

        self.assertIsNone(load_completed_shard(shard, self.entry))

    def test_archive_verifies_before_removing_local_clone(self):
        archive_root = self.root / "nas"
        result = archive_repository(
            self.repo,
            archive_root,
            self.entry,
            allowed_cache_root=self.root / "cache",
            remove_local=True,
        )

        self.assertFalse(self.repo.exists())
        self.assertTrue(Path(result["archive"]).exists())
        self.assertEqual(len(result["sha256"]), 64)

    def test_archive_mismatch_preserves_local_clone(self):
        archive_root = self.root / "nas"
        with mock.patch(
            "authorship.corpus.pinned._sha256_file",
            side_effect=["a" * 64, "b" * 64],
        ):
            with self.assertRaisesRegex(CorpusError, "checksum"):
                archive_repository(
                    self.repo,
                    archive_root,
                    self.entry,
                    allowed_cache_root=self.root / "cache",
                    remove_local=True,
                )

        self.assertTrue(self.repo.exists())

    def test_archive_rejects_repository_outside_cache(self):
        with self.assertRaisesRegex(CorpusError, "outside"):
            archive_repository(
                self.repo,
                self.root / "nas",
                self.entry,
                allowed_cache_root=self.root / "different-cache",
            )

    def test_invalid_shard_metadata_is_ignored(self):
        shard = self.root / "shards" / "owner__project.jsonl"
        shard.parent.mkdir()
        shard.write_text("{}\n")
        shard.with_suffix(".jsonl.meta.json").write_text("{invalid")

        self.assertIsNone(load_completed_shard(shard, self.entry))

    def test_cli_resumes_completed_selected_shard(self):
        manifest_path = Path(__file__).resolve().parents[1] / "study" / "repositories.v1.json"
        manifest = json.loads(manifest_path.read_text())
        entry = manifest["repositories"][0]
        shards = self.root / "shards"
        shard = shards / f"{entry['id'].replace('/', '__')}.jsonl"
        write_completed_shard(shard, [], entry)
        argv = [
            "pinned",
            "--manifest",
            str(manifest_path),
            "--cache",
            str(self.root / "cache"),
            "--shards",
            str(shards),
            "--repo",
            entry["id"],
        ]

        with mock.patch("sys.argv", argv), redirect_stdout(StringIO()) as output:
            result = main()

        self.assertEqual(result, 0)
        self.assertIn(f"{entry['id']}: 0 hunks", output.getvalue())

    def test_target_manifest_loads_only_with_unknown_labels(self):
        path = self.root / "targets.json"
        target = {
            **self.entry,
            "role": "target",
            "label": "unlabeled",
            "content_checksum": f"git-tree-sha1:{self.entry['snapshot']['tree']}",
            "effective_date_range": ["2024-01-01T00:00:00Z", "2026-07-24T23:59:59Z"],
        }
        path.write_text(json.dumps({"repositories": [target]}))
        loaded = load_corpus_manifest(path)
        self.assertEqual(loaded["repositories"][0]["label"], "unlabeled")

        target["label"] = "agent"
        path.write_text(json.dumps({"repositories": [target]}))
        with self.assertRaisesRegex(CorpusError, "unknown"):
            load_corpus_manifest(path)


if __name__ == "__main__":
    unittest.main()
