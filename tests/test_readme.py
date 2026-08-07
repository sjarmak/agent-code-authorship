from pathlib import Path


def test_readme_does_not_link_to_claude_artifact() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    assert "claude.ai/code/artifact" not in readme


def test_readme_does_not_present_obsolete_authorship_estimate_as_current() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    assert "72–89%" not in readme
    assert "The current population share is not identified" in readme
    assert "90.2%" in readme
