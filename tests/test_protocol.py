import json
import unittest
from pathlib import Path

from authorship.protocol import ProtocolError, load_protocol, validate_protocol


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "study" / "protocol.v1.json"


class ProtocolValidationTests(unittest.TestCase):
    def test_canonical_protocol_is_complete(self):
        protocol = load_protocol(PROTOCOL)

        self.assertEqual(validate_protocol(protocol), [])
        self.assertEqual(protocol["estimand"]["languages"], ["Python", "Go"])
        self.assertEqual(protocol["snapshot"]["cutoff"], "2026-07-24T23:59:59Z")
        self.assertEqual(protocol["quantification"]["primary"], "full_score_mixture")
        self.assertTrue(protocol["storage"]["nas"]["offload_after_verification"])
        self.assertEqual(protocol["reporting"]["failed_gate_result"], "not_identified")

    def test_missing_locked_section_is_rejected(self):
        protocol = load_protocol(PROTOCOL)
        del protocol["provenance"]

        errors = validate_protocol(protocol)

        self.assertIn("missing required section: provenance", errors)

    def test_weaker_identification_gate_is_rejected(self):
        protocol = load_protocol(PROTOCOL)
        protocol["identification_gates"]["minimum_repository_groups_per_side"] = 3

        errors = validate_protocol(protocol)

        self.assertIn(
            "identification_gates.minimum_repository_groups_per_side must be >= 5",
            errors,
        )

    def test_repository_role_overlap_is_rejected(self):
        protocol = load_protocol(PROTOCOL)
        protocol["repository_roles"]["allow_role_overlap"] = True

        errors = validate_protocol(protocol)

        self.assertIn("repository role overlap must be forbidden", errors)

    def test_load_raises_one_error_message_for_invalid_protocol(self):
        protocol = json.loads(PROTOCOL.read_text())
        protocol["estimand"]["languages"] = ["Python"]
        broken = ROOT / "study" / "_invalid_protocol.json"
        try:
            broken.write_text(json.dumps(protocol))
            with self.assertRaisesRegex(ProtocolError, "Python and Go"):
                load_protocol(broken)
        finally:
            broken.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
