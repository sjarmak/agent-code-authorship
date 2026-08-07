import json
from pathlib import Path


def test_readme_does_not_link_to_claude_artifact() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    assert "claude.ai/code/artifact" not in readme


def test_readme_does_not_present_obsolete_authorship_estimate_as_current() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    assert "72–89%" not in readme
    assert "The current population share is not identified" in readme
    assert "90.2%" in readme


def test_readme_exposes_public_study_and_reproduction_entrypoints() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    assert "## Read the investigation" in readme
    assert "results/agent-code-authorship-sourcegraph.html" in readme
    assert "## Reproduce the published article" in readme
    assert "python3 -m authorship.build_v3_blog_post" in readme
    assert "python3 -m pip install -r requirements-dev.txt" in readme
    assert "python3 -m pytest -q" in readme


def test_local_agent_metadata_is_ignored_explicitly() -> None:
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()

    for path in (
        "/.agents/",
        "/.beads/",
        "/.claude/",
        "/.codex/",
        "/.dashboard/",
        "/AGENTS.md",
        "/CLAUDE.md",
    ):
        assert path in gitignore


def test_runtime_requirements_include_duckdb() -> None:
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text()

    assert any(line.startswith("duckdb") for line in requirements.splitlines())


def test_browser_checks_have_a_public_dependency_manifest() -> None:
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / "package.json").read_text())

    assert package["scripts"]["test:e2e"] == (
        "playwright test tests/e2e_v3_blog.spec.js"
    )
    assert "@playwright/test" in package["devDependencies"]
    assert "@axe-core/playwright" in package["devDependencies"]
    assert "npm run test:e2e" in (root / "README.md").read_text()
