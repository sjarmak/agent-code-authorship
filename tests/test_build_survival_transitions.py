import subprocess
import tempfile
import unittest
from pathlib import Path

from authorship.build_survival_transitions import first_parent_landing, timeline


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class BuildSurvivalTransitionsTests(unittest.TestCase):
    def test_projects_second_parent_commit_to_first_parent_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            (repo / "base.py").write_text("base = True\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "base")
            git(repo, "checkout", "-q", "-b", "integration")
            git(repo, "checkout", "-q", "-b", "contributor")
            (repo / "agent.py").write_text("agent = True\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "agent")
            attributed = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-q", "integration")
            git(repo, "merge", "-q", "--no-ff", "contributor", "-m", "integrate")
            git(repo, "checkout", "-q", "main")
            git(repo, "merge", "-q", "--no-ff", "integration", "-m", "land")
            landing = git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                first_parent_landing(repo, timeline(repo, landing), attributed),
                landing,
            )


if __name__ == "__main__":
    unittest.main()
