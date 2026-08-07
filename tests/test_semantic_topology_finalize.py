import json
import tempfile
import unittest
from pathlib import Path

from authorship.semantic_topology_finalize import finalize_pilot
from authorship.semantic_topology_protocol import canonical_sha256


class SemanticTopologyFinalizeTests(unittest.TestCase):
    def test_finalize_requires_complete_deep_search_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root, planned_runs=1, materialized_runs=0)

            with self.assertRaisesRegex(ValueError, "Deep Search corpus incomplete"):
                finalize_pilot(root, root / "repos")

    def test_finalize_writes_checksummed_outputs_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root, planned_runs=0, materialized_runs=0)

            manifest = finalize_pilot(root, root / "repos")

            self.assertEqual(manifest["deep_search_run_count"], 0)
            self.assertTrue((root / "results/SEMANTIC_TOPOLOGY_REPORT.v1.md").exists())
            self.assertTrue(
                (root / "results/semantic-topology-candidates.v1.json").exists()
            )
            self.assertRegex(manifest["report_file_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["enriched_record_file_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                canonical_sha256(manifest), manifest["finalization_sha256"]
            )

    def _write_inputs(self, root: Path, planned_runs: int, materialized_runs: int):
        (root / "study").mkdir()
        (root / "results/semantic-topology-deep-search-v1/runs").mkdir(parents=True)
        (root / "results/semantic-topology-records-v1").mkdir(parents=True)
        protocol = {"protocol_sha256": "1" * 64}
        plan = {
            "protocol_sha256": "1" * 64,
            "plan_sha256": "2" * 64,
            "repositories": [],
            "runs": [
                {"run_id": "sha256:" + f"{index + 1:064x}"}
                for index in range(planned_runs)
            ],
        }
        deep = {
            "plan_sha256": "2" * 64,
            "planned_run_count": planned_runs,
            "materialized_run_count": materialized_runs,
            "counts": {},
            "runs": [],
        }
        deep["inventory_sha256"] = canonical_sha256(deep)
        record_inventory = {
            "inventory_sha256": "4" * 64,
            "record_count": 0,
            "failure_count": 0,
        }
        primary = {
            "coverage": 1.0,
            "result_sha256": "5" * 64,
            "fields": {
                "a": {"passes": True},
                "b": {"passes": True},
            },
        }
        supplemental = {
            "coverage": 1.0,
            "observations": 50,
            "kappa": 1.0,
            "passes": True,
            "result_sha256": "6" * 64,
        }
        distinct_author = {
            "coverage": 1.0,
            "observations": 50,
            "kappa": 1.0,
            "passes": True,
            "result_sha256": "7" * 64,
        }
        files = {
            "study/semantic-topology-protocol.v1.json": protocol,
            "study/semantic-topology-pilot-plan.v1.json": plan,
            "results/semantic-topology-deep-search-v1/inventory.v1.json": deep,
            "results/semantic-topology-records-v1/inventory.v1.json": record_inventory,
            "results/semantic-topology-reliability.v1.json": primary,
            "results/semantic-topology-followup-count-reliability.v1.json": supplemental,
            "results/semantic-topology-distinct-author-reliability.v1.json": distinct_author,
        }
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        (
            root
            / "results/semantic-topology-records-v1/semantic-change-records.v1.jsonl"
        ).write_text("")


if __name__ == "__main__":
    unittest.main()
