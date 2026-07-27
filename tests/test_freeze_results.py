import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from authorship.corpus.pinned import FEATURE_SCHEMA
from authorship.freeze_results import FreezeError, freeze


def manifest(repo, role, label, commit):
    return {
        "repositories": [
            {
                "id": repo,
                "role": role,
                "label": label,
                "snapshot": {"commit": commit},
            }
        ]
    }


def shard(root, repo, lang, lines):
    path = root / f"{repo.replace('/', '__')}.jsonl"
    row = {"repo": repo, "lang": lang, "line_count": lines}
    payload = (json.dumps(row) + "\n").encode()
    path.write_bytes(payload)
    path.with_suffix(".jsonl.meta.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "feature_schema": FEATURE_SCHEMA,
            }
        )
    )


class FreezeResultsTests(unittest.TestCase):
    def test_failed_group_gate_suppresses_headline_estimate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs, targets = root / "refs", root / "targets"
            refs.mkdir()
            targets.mkdir()
            shard(refs, "human/ref", "Python", 3000)
            shard(targets, "target/repo", "Python", 4000)
            result = freeze(
                manifest("human/ref", "reference", "human", "a" * 40),
                manifest("target/repo", "target", "unlabeled", "b" * 40),
                refs,
                targets,
                protocol_sha256="c" * 64,
                feature_manifest_sha256="d" * 64,
            )
        self.assertEqual(result["status"], "not_identified")
        self.assertIsNone(result["headline_estimate"])
        self.assertFalse(result["identification_gates"]["python_human_minimum_groups"])

    def test_role_overlap_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs, targets = root / "refs", root / "targets"
            refs.mkdir()
            targets.mkdir()
            shard(refs, "same/repo", "Python", 3000)
            shard(targets, "same/repo", "Python", 3000)
            with self.assertRaisesRegex(FreezeError, "overlap"):
                freeze(
                    manifest("same/repo", "reference", "human", "a" * 40),
                    manifest("same/repo", "target", "unlabeled", "a" * 40),
                    refs,
                    targets,
                    protocol_sha256="c" * 64,
                    feature_manifest_sha256="d" * 64,
                )


if __name__ == "__main__":
    unittest.main()
