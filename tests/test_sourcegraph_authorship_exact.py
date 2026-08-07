from __future__ import annotations

from copy import deepcopy

import pytest

from authorship.sourcegraph_authorship_exact import (
    ExactAuthorshipError,
    exact_hunk_units,
    fetch_commit_metadata,
    fetch_exact_commit_records,
    fetch_root_commit_records,
    fetch_window_commits,
)

REPOSITORY = "paired/repo"
SOURCEGRAPH_NAME = "github.com/sg-evals/paired-repo"
HEAD = "1" * 40
BASE = "2" * 40


def _commit_response(
    oid: str = HEAD, *, parent: str = BASE, committed_at: str = "2025-07-13T15:40:17Z"
) -> dict:
    return {
        "repository": {
            "commit": {
                "oid": oid,
                "committer": {"date": committed_at},
                "parents": [{"oid": parent}],
            }
        }
    }


def test_fetch_commit_metadata_pins_first_parent_and_timestamp() -> None:
    result = fetch_commit_metadata(
        SOURCEGRAPH_NAME,
        HEAD,
        api_runner=lambda *_args, **_kwargs: _commit_response(),
    )

    assert result == {
        "commit_oid": HEAD,
        "committed_at": "2025-07-13T15:40:17Z",
        "first_parent_oid": BASE,
        "parent_count": 1,
        "is_root_commit": False,
    }


def test_fetch_commit_metadata_uses_exact_empty_tree_for_root_commit() -> None:
    response = _commit_response()
    response["repository"]["commit"]["parents"] = []

    result = fetch_commit_metadata(
        SOURCEGRAPH_NAME,
        HEAD,
        api_runner=lambda *_args, **_kwargs: response,
    )

    assert result["first_parent_oid"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert result["parent_count"] == 0
    assert result["is_root_commit"] is True


def test_fetch_exact_commit_records_preserves_sourcegraph_hunk_ranges() -> None:
    calls: list[str] = []

    def runner(query: str, **_variables) -> dict:
        calls.append(query)
        if "changedFiles" in query:
            return {
                "repository": {
                    "commit": {
                        "diff": {
                            "changedFiles": {
                                "nodes": [
                                    {"path": "src/main.go", "status": "MODIFIED"},
                                    {"path": "vendor/skip.go", "status": "ADDED"},
                                    {"path": "src/skip.py", "status": "DELETED"},
                                ],
                                "totalCount": 3,
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                }
            }
        return {
            "repository": {
                "commit": {
                    "diff": {
                        "fileDiffs": {
                            "nodes": [
                                {
                                    "oldPath": "src/main.go",
                                    "newPath": "src/main.go",
                                    "hunks": [
                                        {
                                            "body": " line one\n-old\n+new\n line two\n",
                                            "newRange": {
                                                "startLine": 10,
                                                "lines": 3,
                                            },
                                        }
                                    ],
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            }
        }

    records = fetch_exact_commit_records(
        SOURCEGRAPH_NAME,
        BASE,
        HEAD,
        "Go",
        api_runner=runner,
    )

    assert records == [
        {
            "path": "src/main.go",
            "hunks": [
                {
                    "body": " line one\n-old\n+new\n line two\n",
                    "new_range": {"start_line": 10, "lines": 3},
                }
            ],
        }
    ]
    assert len(calls) == 2


def test_fetch_root_commit_records_materializes_pinned_blobs_as_added_files() -> None:
    def runner(query: str, **variables) -> dict:
        if "files(recursive:true)" in query:
            return {
                "repository": {
                    "commit": {
                        "tree": {
                            "files": [
                                {"path": "pkg/main.py"},
                                {"path": "vendor/skip.py"},
                                {"path": "pkg/skip.go"},
                            ]
                        }
                    }
                }
            }
        assert variables["path"] == "pkg/main.py"
        return {
            "repository": {"commit": {"blob": {"content": "value = 1\nprint(value)\n"}}}
        }

    records = fetch_root_commit_records(
        SOURCEGRAPH_NAME,
        HEAD,
        "Python",
        api_runner=runner,
    )

    assert records == [
        {
            "path": "pkg/main.py",
            "hunks": [
                {
                    "body": "+value = 1\n+print(value)\n",
                    "new_range": {"start_line": 1, "lines": 2},
                }
            ],
        }
    ]


def test_exact_hunk_units_have_concrete_identity_features_and_hashes() -> None:
    records = [
        {
            "path": "pkg/example.py",
            "hunks": [
                {
                    "body": (
                        " context\n-old = 1\n"
                        "+def render(value: int) -> str:\n"
                        '+    return f"{value}"\n+\n'
                    ),
                    "new_range": {"start_line": 20, "lines": 4},
                }
            ],
        }
    ]

    units = exact_hunk_units(
        repository_id=REPOSITORY,
        sourcegraph_name=SOURCEGRAPH_NAME,
        commit_oid=HEAD,
        first_parent_oid=BASE,
        committed_at="2025-07-13T15:40:17Z",
        language="Python",
        records=records,
        authorship_role="agent",
        evidence_tier="confirmed_agent_commit",
    )

    assert len(units) == 1
    unit = units[0]
    assert unit["line_numbers"] == [21, 22, 23]
    assert unit["line_count"] == 3
    assert unit["path_type"] == "source"
    assert unit["code_age_days"] == 0
    assert unit["calendar_time"] == "2025-07-13T15:40:17Z"
    assert len(unit["hunk_sha256"]) == 64
    assert len(unit["content_sha256"]) == 64
    assert unit["feature_values"]["typehint_rate"] > 0
    assert unit["feature_values"]["fstring_rate"] > 0


def test_exact_hunk_units_reject_invalid_sourcegraph_range() -> None:
    records = [
        {
            "path": "pkg/example.py",
            "hunks": [
                {
                    "body": " context\n+added\n",
                    "new_range": {"start_line": 20, "lines": 99},
                }
            ],
        }
    ]

    with pytest.raises(ExactAuthorshipError, match="range"):
        exact_hunk_units(
            repository_id=REPOSITORY,
            sourcegraph_name=SOURCEGRAPH_NAME,
            commit_oid=HEAD,
            first_parent_oid=BASE,
            committed_at="2025-07-13T15:40:17Z",
            language="Python",
            records=records,
            authorship_role="human",
            evidence_tier="H3_contemporary_pre_adoption",
        )


@pytest.mark.parametrize(
    ("committed_at", "message"),
    [
        (None, "timestamp is missing"),
        ("not-a-date", "timestamp is invalid"),
        ("2025-01-01T00:00:00", "lacks timezone"),
    ],
)
def test_exact_hunk_units_rejects_noncanonical_timestamps(
    committed_at, message
) -> None:
    with pytest.raises(ExactAuthorshipError, match=message):
        exact_hunk_units(
            repository_id=REPOSITORY,
            sourcegraph_name=SOURCEGRAPH_NAME,
            commit_oid=HEAD,
            first_parent_oid=BASE,
            committed_at=committed_at,
            language="Go",
            records=[],
            authorship_role="agent",
            evidence_tier="confirmed_agent_commit",
        )


def test_exact_hunk_units_rejects_invalid_role_identity() -> None:
    with pytest.raises(ExactAuthorshipError, match="identity"):
        exact_hunk_units(
            repository_id=REPOSITORY,
            sourcegraph_name=SOURCEGRAPH_NAME,
            commit_oid=HEAD,
            first_parent_oid=BASE,
            committed_at="2025-01-01T00:00:00Z",
            language="Go",
            records=[],
            authorship_role="unknown",
            evidence_tier="confirmed_agent_commit",
        )


def test_window_commits_paginates_and_applies_half_open_dates() -> None:
    pages = [
        {
            "nodes": [
                {
                    "oid": "3" * 40,
                    "committer": {"date": "2025-07-13T15:40:17Z"},
                    "parents": [{"oid": "4" * 40}],
                },
                {
                    "oid": "5" * 40,
                    "committer": {"date": "2025-07-01T00:00:00Z"},
                    "parents": [{"oid": "6" * 40}],
                },
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "2"},
        },
        {
            "nodes": [
                {
                    "oid": "7" * 40,
                    "committer": {"date": "2025-01-14T15:40:17Z"},
                    "parents": [{"oid": "8" * 40}],
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": "3"},
        },
    ]
    calls = 0

    def runner(_query: str, **_variables) -> dict:
        nonlocal calls
        page = pages[calls]
        calls += 1
        return {"repository": {"commit": {"ancestors": deepcopy(page)}}}

    commits = fetch_window_commits(
        SOURCEGRAPH_NAME,
        HEAD,
        start_inclusive="2025-01-14T15:40:17Z",
        end_exclusive="2025-07-13T15:40:17Z",
        api_runner=runner,
    )

    assert [row["commit_oid"] for row in commits] == ["7" * 40, "5" * 40]
    assert calls == 2


def test_window_commits_rejects_nonadvancing_pagination() -> None:
    connection = {
        "nodes": [],
        "pageInfo": {"hasNextPage": True, "endCursor": "same"},
    }

    with pytest.raises(ExactAuthorshipError, match="did not advance"):
        fetch_window_commits(
            SOURCEGRAPH_NAME,
            HEAD,
            start_inclusive="2025-01-01T00:00:00Z",
            end_exclusive="2025-02-01T00:00:00Z",
            api_runner=lambda *_args, **_kwargs: {
                "repository": {"commit": {"ancestors": connection}}
            },
        )
