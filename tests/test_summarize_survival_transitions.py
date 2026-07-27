import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from authorship.summarize_survival_transitions import summarize_repository


class SummarizeSurvivalTransitionsTests(unittest.TestCase):
    def test_requires_every_line_at_every_horizon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transitions").mkdir()
            (root / "structural-events").mkdir()
            rows = [
                {"line_id": "line", "horizon_days": horizon, "state": "unchanged"}
                for horizon in (30, 90, 180, 365)
            ]
            payload = "".join(json.dumps(row) + "\n" for row in rows)
            (root / "transitions" / "o__r.jsonl").write_text(payload)
            (root / "structural-events" / "o__r.jsonl").write_text("")

            result = summarize_repository(
                {"repository_id": "o/r", "line_count": 1}, root
            )

            self.assertEqual(result["transition_count"], 4)
            self.assertEqual(
                result["transition_sha256"],
                hashlib.sha256(payload.encode()).hexdigest(),
            )

    def test_rejects_incomplete_horizon_cardinality(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transitions").mkdir()
            (root / "structural-events").mkdir()
            (root / "transitions" / "o__r.jsonl").write_text(
                json.dumps(
                    {"line_id": "line", "horizon_days": 30, "state": "unchanged"}
                )
                + "\n"
            )
            (root / "structural-events" / "o__r.jsonl").write_text("")

            with self.assertRaises(ValueError):
                summarize_repository(
                    {"repository_id": "o/r", "line_count": 1}, root
                )


if __name__ == "__main__":
    unittest.main()
