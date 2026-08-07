import unittest

from authorship.semantic_control_matching import (
    FORBIDDEN_OUTCOME_KEYS,
    classify_task,
    freeze_matches,
    validate_outcome_blind,
)


def unit(identifier, *, treated, task="feature", path="source", added=3, day=0):
    return {
        "unit_id": identifier,
        "exposure": "treated" if treated else "control_candidate",
        "repository": "example/repo",
        "commit": identifier[-40:].rjust(40, "0"),
        "diff_base": "1" * 40,
        "path": f"src/{identifier}.py",
        "hunk": {
            "new_start": 1,
            "new_count": added,
            "added_line_count": added,
            "deleted_line_count": 1,
        },
        "covariates": {
            "repository": "example/repo",
            "path_class": path,
            "task_class": task,
            "language": "Python",
            "log1p_added_lines": float(added),
            "log1p_deleted_lines": 1.0,
            "file_age_days": 100.0,
            "prior_path_commit_count_180d": 2.0,
            "repository_commit_count_30d": 20.0,
            "introduced_at_epoch": float(day) * 86400,
        },
        "provenance": {"outcomes_consulted": False},
    }


class SemanticControlMatchingTests(unittest.TestCase):
    def test_task_classification_is_deterministic_and_path_aware(self):
        self.assertEqual(classify_task("fix parser crash", "src/parser.py"), "bug_fix")
        self.assertEqual(
            classify_task("update guide", "docs/guide.md"), "documentation"
        )
        self.assertEqual(classify_task("add coverage", "tests/test_api.py"), "test")
        self.assertEqual(classify_task("refactor visitor", "src/visit.py"), "refactor")
        self.assertEqual(classify_task("add streaming API", "src/api.py"), "feature")
        self.assertEqual(classify_task("bump dependency", "go.mod"), "maintenance")
        self.assertEqual(classify_task("miscellaneous", "src/x.py"), "other")

    def test_matcher_is_deterministic_without_replacement(self):
        treated = [
            unit("treated-a", treated=True),
            unit("treated-b", treated=True, added=4),
        ]
        candidates = [
            unit("candidate-a", treated=False, added=3),
            unit("candidate-b", treated=False, added=4),
            unit("candidate-c", treated=False, added=20),
        ]
        first = freeze_matches(treated, candidates, caliper=2.5)
        second = freeze_matches(
            list(reversed(treated)), list(reversed(candidates)), caliper=2.5
        )
        self.assertEqual(first, second)
        selected = [item["control_unit_id"] for item in first["matches"]]
        self.assertEqual(len(selected), len(set(selected)))

    def test_exact_task_match_precedes_fallback(self):
        treated = [unit("treated", treated=True, task="feature")]
        candidates = [
            unit("exact", treated=False, task="feature", added=20),
            unit("fallback", treated=False, task="bug_fix", added=3),
        ]
        result = freeze_matches(treated, candidates, caliper=100)
        self.assertEqual(result["matches"][0]["control_unit_id"], "exact")
        self.assertEqual(result["matches"][0]["match_tier"], "exact_task")

    def test_tight_caliper_records_unmatched_reason(self):
        treated = [unit("treated", treated=True, added=1)]
        candidates = [unit("candidate", treated=False, added=1000)]
        result = freeze_matches(treated, candidates, caliper=0.01)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["unmatched"][0]["reason"], "outside_caliper")

    def test_outcome_leakage_fails_closed_recursively(self):
        row = unit("candidate", treated=False)
        row["nested"] = {"subsequent_change_present": True}
        errors = validate_outcome_blind([row])
        self.assertTrue(errors)
        self.assertIn("subsequent_change_present", FORBIDDEN_OUTCOME_KEYS)
