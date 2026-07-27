import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from authorship.survival_lines import (
    commit_parents_many,
    extract_added_lines,
    extract_first_parent_many,
    pr_diff_base,
)


def git(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


class SurvivalLineTests(unittest.TestCase):
    def test_extracts_nonblank_language_lines_and_skips_vendor(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            (repo / "app.py").write_text("old = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "vendor").mkdir()
            (repo / "app.py").write_text("old = 1\n\nnew = 2\n")
            (repo / "vendor" / "ignored.py").write_text("ignored = True\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "change")
            head = git(repo, "rev-parse", "HEAD")

            lines = extract_added_lines(repo, base, head, "Python")

            self.assertEqual(
                [(line["path"], line["line_number"], line["text"]) for line in lines],
                [("app.py", 3, "new = 2")],
            )

    def test_diff_base_uses_first_parent_for_merge_and_earliest_rebased_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            base = self._commit(repo, "base.py", "base")
            first = self._commit(repo, "one.py", "one")
            last = self._commit(repo, "two.py", "two")
            positions = {base: 0, first: 1, last: 2}

            self.assertEqual(
                pr_diff_base(repo, last, [first, last], positions), base
            )

            git(repo, "checkout", "-q", "-b", "feature", base)
            feature = self._commit(repo, "feature.py", "feature")
            git(repo, "checkout", "-q", "main")
            git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")
            merge = git(repo, "rev-parse", "HEAD")
            self.assertEqual(pr_diff_base(repo, merge, [feature], positions), last)
            bulk = extract_first_parent_many(repo, [first, last], "Python")
            self.assertEqual(bulk[first][0]["text"], "one = True")
            self.assertEqual(bulk[last][0]["text"], "two = True")
            parents = commit_parents_many(repo, [first, last, merge])
            self.assertEqual(parents[first], [base])
            self.assertEqual(parents[last], [first])
            self.assertEqual(parents[merge][0], last)

    def test_rename_uses_destination_path_and_binary_python_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            (repo / "old.py").write_text("value = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "mv", "old.py", "new.py")
            (repo / "new.py").write_text("value = 1\nadded = 2\n")
            (repo / "binary.py").write_bytes(bytes([0, 1, 2]))
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "rename")
            head = git(repo, "rev-parse", "HEAD")

            lines = extract_added_lines(repo, base, head, "Python")

            self.assertEqual(
                [(line["path"], line["text"]) for line in lines],
                [("new.py", "added = 2")],
            )

    @staticmethod
    def _commit(repo: Path, filename: str, text: str) -> str:
        (repo / filename).write_text(f"{text} = True\n")
        git(repo, "add", filename)
        git(repo, "commit", "-q", "-m", text, env=os.environ)
        return git(repo, "rev-parse", "HEAD")


if __name__ == "__main__":
    unittest.main()
