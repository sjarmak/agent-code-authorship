import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from authorship.survival_git import (
    clone_or_fetch,
    git as survival_git,
    git_with_stdin,
    inspect_repository,
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


class SurvivalGitTests(unittest.TestCase):
    def test_git_with_stdin_resolves_many_revisions_in_one_process(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            first = self._commit(repo, "first", "2025-01-01T00:00:00Z")
            second = self._commit(repo, "second", "2025-02-01T00:00:00Z")

            output = git_with_stdin(
                repo,
                f"{first}\n{second}\n",
                "log",
                "--no-walk",
                "--format=%H%x00%cI",
                "--stdin",
            )

            assert {line.split("\0")[0] for line in output.splitlines()} == {
                first,
                second,
            }

    def test_git_replaces_invalid_utf8_in_historical_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            (repo / "legacy.py").write_bytes(b"value = '" + bytes([0xB2]) + b"'\n")
            git(repo, "add", "legacy.py")
            git(repo, "commit", "-q", "-m", "legacy bytes")

            payload = survival_git(repo, "show", "HEAD:legacy.py")

            self.assertIn("\ufffd", payload)

    def test_cutoff_snapshot_and_ancestry_exclude_future_and_side_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            old = self._commit(repo, "old", "2025-01-01T00:00:00Z")
            git(repo, "checkout", "-q", "-b", "side")
            side = self._commit(repo, "side", "2025-02-01T00:00:00Z")
            git(repo, "checkout", "-q", "main")
            future = self._commit(repo, "future", "2027-01-01T00:00:00Z")

            result = inspect_repository(
                repo,
                default_branch="main",
                cutoff="2026-07-24T23:59:59Z",
                attributed_commits=[old, side, future, "f" * 40],
            )

            self.assertEqual(result["cutoff_commit"], old)
            self.assertEqual(result["reachable_attributed_commits"], [old])
            self.assertCountEqual(
                result["unreachable_attributed_commits"], [future, side]
            )
            self.assertEqual(result["missing_attributed_commits"], ["f" * 40])

    def test_clone_or_fetch_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            git(source, "init", "-q", "-b", "main")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.com")
            first = self._commit(source, "first", "2025-01-01T00:00:00Z")
            remote = root / "remote.git"
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", str(source), str(remote)],
                check=True,
            )
            destination = root / "cache"

            clone_or_fetch(f"file://{root / 'remote'}", destination)

            self.assertEqual(git(destination, "rev-parse", "origin/main"), first)
            second = self._commit(source, "second", "2025-02-01T00:00:00Z")
            git(source, "push", "-q", str(remote), "main")
            clone_or_fetch(f"file://{root / 'remote'}", destination)
            self.assertEqual(git(destination, "rev-parse", "origin/main"), second)

    @staticmethod
    def _commit(repo: Path, text: str, date: str) -> str:
        path = repo / f"{text}.py"
        path.write_text(f"print({text!r})\n")
        git(repo, "add", path.name)
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        git(repo, "commit", "-q", "-m", text, env=environment)
        return git(repo, "rev-parse", "HEAD")


if __name__ == "__main__":
    unittest.main()
