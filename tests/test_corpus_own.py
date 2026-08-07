import unittest
from pathlib import Path
from unittest.mock import patch

from authorship.corpus.own import code_files


class OwnCorpusTests(unittest.TestCase):
    def test_code_files_excludes_generated_and_vendored_paths(self):
        tracked = "\n".join(
            [
                "src/main.py",
                "vendor/copied.py",
                "generated/client.go",
                "README.md",
            ]
        )

        with patch("authorship.corpus.own._git", return_value=tracked):
            paths = code_files(Path("/tmp/repository"))

        self.assertEqual(paths, ["src/main.py"])


if __name__ == "__main__":
    unittest.main()
