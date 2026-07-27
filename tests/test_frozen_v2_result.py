import hashlib
import json
import unittest
from pathlib import Path

from authorship.corpus.pinned import FEATURE_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FrozenV2ResultTests(unittest.TestCase):
    def setUp(self):
        self.result_path = ROOT / "results" / "replication.v2.json"
        self.result = json.loads(self.result_path.read_text())

    def test_report_pins_exact_machine_result(self):
        report = (ROOT / "results" / "REPORT.v2.md").read_text()

        self.assertIn(sha256(self.result_path), report)
        self.assertEqual(self.result["status"], "not_identified")
        self.assertIsNone(self.result["headline_estimate"])

    def test_every_pinned_input_hash_matches_current_artifact(self):
        paths = {
            "protocol_v1": ROOT / "study" / "protocol.v1.json",
            "protocol_amendment_v2": ROOT / "study" / "protocol-amendment.v2.json",
            "features_v1": ROOT / "study" / "features.v1.json",
            "reference_manifest_v1": ROOT / "study" / "repositories.v1.json",
            "target_manifest_v1": ROOT / "study" / "targets.v1.json",
            "candidate_frame_v2": ROOT / "study" / "reference-candidates.v2.json",
            "attestation_ledger_v2": ROOT / "study" / "attestations.v2.json",
            "execution_decision_v2": ROOT / "study" / "execution-decision.v2.json",
            "human_audit_batch1_v2": (
                ROOT / "study" / "human-candidate-audit-batch1.v2.json"
            ),
            "human_audit_batch2_v2": (
                ROOT / "study" / "human-candidate-audit-batch2.v2.json"
            ),
            "agent_go_audit_v2": (
                ROOT / "study" / "agent-go-candidate-audit.v2.json"
            ),
            "reference_manifest_v2": ROOT / "study" / "repositories.v2.json",
            "role_separation_v2": ROOT / "study" / "role-separation.v2.json",
            "replication_v1": ROOT / "results" / "replication.v1.json",
            "freeze_implementation": (
                ROOT / "authorship" / "freeze_nonidentification.py"
            ),
            "role_separation_implementation": (
                ROOT / "authorship" / "role_separation.py"
            ),
            "replication_implementation": ROOT / "authorship" / "replication.py",
            "rebuild_implementation": (
                ROOT / "authorship" / "rebuild_v2_corpus.py"
            ),
        }

        self.assertEqual(
            self.result["artifact_sha256"],
            {name: sha256(path) for name, path in sorted(paths.items())},
        )

    def test_rebuilt_shards_match_frozen_separation_report(self):
        report = json.loads(
            (ROOT / "study" / "role-separation.v2.json").read_text()
        )
        root = Path(report["output_reference_shards"])
        for repository, summary in report["repositories"].items():
            shard = root / f"{repository.replace('/', '__')}.jsonl"
            meta = json.loads(shard.with_suffix(".jsonl.meta.json").read_text())
            self.assertEqual(sha256(shard), summary["shard_sha256"])
            self.assertEqual(meta["sha256"], summary["shard_sha256"])
            self.assertEqual(meta["feature_schema"], FEATURE_SCHEMA)

    def test_no_numerical_gate_is_claimed_from_failed_reference_precondition(self):
        gates = self.result["locked_quantitative_gates"]

        self.assertFalse(self.result["reference_diversity_gate_passed"])
        self.assertTrue(
            all(gate["status"] in {"not_run", "not_evaluable"} for gate in gates.values())
        )
        self.assertTrue(all(gate["passed"] is False for gate in gates.values()))


if __name__ == "__main__":
    unittest.main()
