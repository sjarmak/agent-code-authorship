import json
import unittest
from pathlib import Path

from authorship.rebuild_v2_corpus import build_v2_manifest


ROOT = Path(__file__).resolve().parents[1]


class RebuildV2CorpusTests(unittest.TestCase):
    def test_no_outreach_manifest_keeps_only_admissible_agent_references(self):
        v1 = json.loads((ROOT / "study" / "repositories.v1.json").read_text())

        result = build_v2_manifest(
            v1,
            protocol_sha256="a" * 64,
        )

        self.assertEqual(result["manifest_version"], 2)
        self.assertEqual(len(result["repositories"]), 9)
        self.assertEqual({entry["label"] for entry in result["repositories"]}, {"agent"})
        self.assertEqual(
            {entry["role"] for entry in result["repositories"]},
            {"reference", "dedicated_validation"},
        )
        self.assertFalse(result["selection"]["model_outputs_consulted"])
        self.assertEqual(
            result["selection"]["human_frame_status"],
            "closed_without_solicitation_no_admissible_labels",
        )


if __name__ == "__main__":
    unittest.main()
