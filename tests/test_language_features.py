import unittest
import hashlib
import json
from pathlib import Path

from authorship.features import NAMES
from authorship.language_features import (
    PRIMARY_SCHEMA_VERSION,
    code_kind,
    primary_features,
    primary_vector,
)


class LanguageFeatureTests(unittest.TestCase):
    def test_python_tokenizer_ignores_comment_markers_inside_strings(self):
        result = primary_features(
            ['url = "https://example.test/#fragment"', "value = 1  # actual"],
            "Python",
        )

        self.assertEqual(result["comment_line_rate"], 0.0)
        self.assertEqual(result["inline_comment_rate"], 0.5)
        self.assertEqual(result["comment_words_mean"], 1.0)

    def test_python_ast_identifies_real_docstrings_not_assigned_strings(self):
        with_docstring = primary_features(
            ['"""Module documentation."""', "value = 1"], "Python"
        )
        assigned = primary_features(
            ['value = """Not a docstring."""', "other = 2"], "Python"
        )

        self.assertEqual(with_docstring["docstring_present"], 1.0)
        self.assertEqual(assigned["docstring_present"], 0.0)

    def test_incomplete_python_hunk_falls_back_without_crashing(self):
        result = primary_features(["def broken(:", "    # still a comment"], "Python")

        self.assertEqual(result["comment_line_rate"], 0.5)
        self.assertEqual(result["docstring_present"], 0.0)

    def test_go_lexer_handles_raw_strings_and_real_line_comments(self):
        result = primary_features(
            ["value := `// not a comment`", "next := 1 // real comment"], "Go"
        )

        self.assertEqual(result["comment_line_rate"], 0.0)
        self.assertEqual(result["inline_comment_rate"], 0.5)
        self.assertEqual(result["comment_words_mean"], 2.0)

    def test_go_block_comments_are_counted_by_source_line(self):
        result = primary_features(
            ["/* first line", "second line */", "value := 1"], "Go"
        )

        self.assertAlmostEqual(result["comment_line_rate"], 2 / 3)
        self.assertEqual(result["comment_words_mean"], 2.0)

    def test_go_documentation_comment_preceding_declaration_is_detected(self):
        result = primary_features(
            ["// Widget represents a widget.", "type Widget struct {}"], "Go"
        )

        self.assertEqual(result["docstring_present"], 1.0)

    def test_primary_schema_preserves_frozen_feature_order(self):
        result = primary_features(["value = 1"], "Python")

        self.assertEqual(PRIMARY_SCHEMA_VERSION, 1)
        self.assertEqual(tuple(result), NAMES)
        self.assertEqual(len(primary_vector(["value = 1"], "Python")), len(NAMES))

    def test_code_kind_is_stratification_metadata_not_a_feature(self):
        self.assertEqual(code_kind("pkg/service.py"), "production")
        self.assertEqual(code_kind("pkg/tests/test_service.py"), "test")
        self.assertNotIn("code_kind", NAMES)

    def test_frozen_schema_matches_implementation(self):
        root = Path(__file__).resolve().parents[1]
        frozen = json.loads((root / "study" / "features.v1.json").read_text())

        self.assertEqual(frozen["names"], list(NAMES))
        self.assertEqual(
            frozen["implementation_sha256"],
            hashlib.sha256(
                (root / "authorship" / "language_features.py").read_bytes()
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
