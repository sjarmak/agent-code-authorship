import unittest

from authorship.survival_lineage import (
    advance_states,
    classify_lines,
    parse_patch,
    parse_name_status,
    select_horizon,
)


def intro(line, text, path="app.py"):
    return {
        "path": path,
        "line_number": line,
        "text": text,
        "content_sha256": "unused",
    }


class SurvivalLineageTests(unittest.TestCase):
    def test_insertion_shifts_unchanged_line(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,0 +2 @@
+inserted = True
"""
        states = classify_lines([intro(3, "target = 1")], parse_patch(patch))

        self.assertEqual(states[0]["state"], "unchanged")
        self.assertEqual(states[0]["horizon_line_number"], 4)

    def test_unique_similar_replacement_is_structural_candidate(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +3 @@
-result = calculate_total(items)
+result = calculate_sum(items)
"""
        states = classify_lines(
            [intro(3, "result = calculate_total(items)")], parse_patch(patch)
        )

        self.assertEqual(states[0]["state"], "modified_candidate")
        self.assertGreaterEqual(states[0]["structural_score"], 0.8)
        self.assertEqual(states[0]["horizon_text"], "result = calculate_sum(items)")

    def test_pure_removal_is_deleted(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +2,0 @@
-obsolete = True
"""
        states = classify_lines([intro(3, "obsolete = True")], parse_patch(patch))

        self.assertEqual(states[0]["state"], "deleted")

    def test_removal_with_possible_cross_file_move_is_not_forced_deleted(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +2,0 @@
-moved_value = calculate(items)
diff --git a/new.py b/new.py
--- a/new.py
+++ b/new.py
@@ -1,0 +2 @@
+moved_value = calculate(items)
"""
        states = classify_lines(
            [intro(3, "moved_value = calculate(items)")], parse_patch(patch)
        )

        self.assertEqual(states[0]["state"], "unchanged")
        self.assertEqual(states[0]["horizon_path"], "new.py")

    def test_rename_maps_unchanged_line_to_destination(self):
        patch = """diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""
        states = classify_lines(
            [intro(5, "stable = True", "old.py")], parse_patch(patch)
        )

        self.assertEqual(states[0]["state"], "unchanged")
        self.assertEqual(states[0]["horizon_path"], "new.py")

    def test_ambiguous_replacement_is_unobservable(self):
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +3,2 @@
-total = calculate(items)
+total = calculate(a)
+total = calculate(b)
"""
        states = classify_lines(
            [intro(3, "total = calculate(items)")], parse_patch(patch)
        )

        self.assertEqual(states[0]["state"], "unobservable")
        self.assertEqual(states[0]["reason"], "ambiguous_replacement")
        self.assertEqual(states[0]["horizon_text"], "total = calculate(a)")
        self.assertEqual(states[0]["horizon_path"], "app.py")
        self.assertEqual(states[0]["horizon_line_number"], 3)

    def test_horizon_selection_is_cutoff_aware_and_descends_from_merge(self):
        timeline = [
            ("a" * 40, "2025-01-01T00:00:00Z"),
            ("b" * 40, "2025-02-15T00:00:00Z"),
            ("c" * 40, "2025-05-01T00:00:00Z"),
        ]

        observed = select_horizon(
            timeline,
            merge_commit="a" * 40,
            merged_at="2025-01-01T00:00:00Z",
            days=90,
            cutoff="2025-12-31T23:59:59Z",
        )
        censored = select_horizon(
            timeline,
            merge_commit="c" * 40,
            merged_at="2025-05-01T00:00:00Z",
            days=365,
            cutoff="2025-12-31T23:59:59Z",
        )

        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["commit"], "b" * 40)
        self.assertEqual(censored["status"], "right_censored")

    def test_state_machine_retains_modified_state_then_terminates_on_delete(self):
        states = {
            "line-1": {
                "state": "unchanged",
                "path": "app.py",
                "line_number": 3,
                "text": "result = calculate_total(items)",
            }
        }
        modified_patch = parse_patch(
            """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +3 @@
-result = calculate_total(items)
+result = calculate_sum(items)
"""
        )
        deletion_patch = parse_patch(
            """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3 +2,0 @@
-result = calculate_sum(items)
"""
        )

        first_events = advance_states(states, modified_patch, commit="b" * 40)
        second_events = advance_states(states, deletion_patch, commit="c" * 40)

        self.assertEqual(states["line-1"]["state"], "deleted")
        self.assertEqual(first_events[0]["decision"], "modified_candidate")
        self.assertEqual(second_events[0]["decision"], "deleted")
        self.assertEqual(states["line-1"]["terminal_commit"], "c" * 40)

    def test_name_status_parser_includes_both_sides_of_rename(self):
        payload = (
            f"__COMMIT__{'a' * 40}\n"
            "M\tapp.py\n"
            "R100\told.py\tnew.py\n"
            f"__COMMIT__{'b' * 40}\n"
            "D\tgone.go\n"
        )

        parsed = parse_name_status(payload)

        self.assertEqual(parsed["a" * 40], {"app.py", "old.py", "new.py"})
        self.assertEqual(parsed["b" * 40], {"gone.go"})

    def test_malformed_git_path_does_not_crash_parser(self):
        parsed = parse_patch(
            'diff --git "a/broken.py b/broken.py\n'
            '--- "a/broken.py\n'
            '+++ "b/broken.py\n'
        )

        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0]["old_path"])

    def test_oversized_cross_file_search_is_unobservable(self):
        additions = "\n".join(f"+candidate_{index} = {index}" for index in range(2001))
        patch = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -3 +2,0 @@\n"
            "-moved = calculate(items)\n"
            "diff --git a/generated.py b/generated.py\n"
            "--- a/generated.py\n"
            "+++ b/generated.py\n"
            "@@ -1,0 +1,2001 @@\n"
            f"{additions}\n"
        )

        state = classify_lines(
            [intro(3, "moved = calculate(items)")], parse_patch(patch)
        )[0]

        self.assertEqual(state["state"], "unobservable")
        self.assertEqual(state["reason"], "cross_file_candidate_space_too_broad")


if __name__ == "__main__":
    unittest.main()
