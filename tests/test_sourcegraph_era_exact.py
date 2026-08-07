import pytest

from authorship.sourcegraph_era_exact import (
    eligible_changed_paths,
    fetch_changed_files,
    fetch_exact_period_summary,
)


def test_changed_files_adapter_uses_optimized_connection_and_paginates():
    calls = []

    def fake_api(_graphql_query, **variables):
        calls.append(variables)
        after = variables.get("after")
        nodes = (
            [{"path": "src/a.py", "status": "MODIFIED"}]
            if after is None
            else [{"path": "src/b.py", "status": "ADDED"}]
        )
        return {
            "repository": {
                "commit": {
                    "diff": {
                        "changedFiles": {
                            "nodes": nodes,
                            "totalCount": 2,
                            "pageInfo": {
                                "hasNextPage": after is None,
                                "endCursor": "next" if after is None else None,
                            },
                        }
                    }
                }
            }
        }

    files = fetch_changed_files(
        "github.com/sg-evals/org-repo",
        "0" * 40,
        "1" * 40,
        api_runner=fake_api,
    )

    assert files == [
        {"path": "src/a.py", "status": "MODIFIED"},
        {"path": "src/b.py", "status": "ADDED"},
    ]
    assert calls == [
        {
            "repo": "github.com/sg-evals/org-repo",
            "rev": "1" * 40,
            "base": "0" * 40,
        },
        {
            "repo": "github.com/sg-evals/org-repo",
            "rev": "1" * 40,
            "base": "0" * 40,
            "after": "next",
        },
    ]


def test_eligible_paths_are_exact_language_and_exclude_generated_vendor():
    files = [
        {"path": "src/a.py", "status": "MODIFIED"},
        {"path": "src/a.pyi", "status": "MODIFIED"},
        {"path": "vendor/b.py", "status": "MODIFIED"},
        {"path": "src/c.go", "status": "ADDED"},
        {"path": "src/deleted.py", "status": "DELETED"},
    ]

    assert eligible_changed_paths(files, "Python") == ["src/a.py"]
    assert eligible_changed_paths(files, "Go") == ["src/c.go"]
    with pytest.raises(ValueError, match="Python or Go"):
        eligible_changed_paths(files, "Rust")


def test_changed_file_cursor_must_advance():
    def fake_api(_graphql_query, **_variables):
        return {
            "repository": {
                "commit": {
                    "diff": {
                        "changedFiles": {
                            "nodes": [],
                            "totalCount": 1,
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": None,
                            },
                        }
                    }
                }
            }
        }

    with pytest.raises(RuntimeError, match="did not advance"):
        fetch_changed_files(
            "github.com/sg-evals/org-repo",
            "0" * 40,
            "1" * 40,
            api_runner=fake_api,
        )


def test_exact_period_fetches_all_eligible_paths_in_one_batch():
    calls = []

    def fake_api(graphql_query, **variables):
        calls.append((graphql_query, variables))
        if "EraPanelChangedFiles" in graphql_query:
            return {
                "repository": {
                    "commit": {
                        "diff": {
                            "changedFiles": {
                                "nodes": [
                                    {"path": "src/a.py", "status": "ADDED"},
                                    {
                                        "path": "tests/test_a.py",
                                        "status": "MODIFIED",
                                    },
                                ],
                                "totalCount": 2,
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
                                    "oldPath": None,
                                    "newPath": "src/a.py",
                                    "hunks": [{"body": "+def a():\n+    return 1\n"}],
                                },
                                {
                                    "oldPath": "tests/test_a.py",
                                    "newPath": "tests/test_a.py",
                                    "hunks": [{"body": "+assert a() == 1\n"}],
                                },
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

    summary = fetch_exact_period_summary(
        "github.com/sg-evals/org-repo",
        "0" * 40,
        "1" * 40,
        "Python",
        period=8100,
        api_runner=fake_api,
    )

    assert summary["introduced_code_line_count"] == 3
    assert summary["eligible_path_count"] == 2
    assert summary["materialization_design"] == "exact_net_quarter_path_batches"
    assert summary["path_type_counts"] == {"source": 1, "test": 1}
    assert summary["code_age_days"] == 0
    assert len(summary["raw_diff_sha256"]) == 64
    assert set(summary["feature_values"])
    assert calls[1][1]["paths"] == ["src/a.py", "tests/test_a.py"]
    assert len(calls) == 2


def test_exact_path_batches_are_bounded_to_64():
    paths = [f"src/file_{index:03d}.py" for index in range(65)]
    batch_sizes = []

    def fake_api(graphql_query, **variables):
        if "EraPanelChangedFiles" in graphql_query:
            return {
                "repository": {
                    "commit": {
                        "diff": {
                            "changedFiles": {
                                "nodes": [
                                    {"path": path, "status": "MODIFIED"}
                                    for path in paths
                                ],
                                "totalCount": len(paths),
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                }
            }
        batch_sizes.append(len(variables["paths"]))
        return {
            "repository": {
                "commit": {
                    "diff": {
                        "fileDiffs": {
                            "nodes": [
                                {
                                    "oldPath": path,
                                    "newPath": path,
                                    "hunks": [{"body": "+x = 1\n"}],
                                }
                                for path in variables["paths"]
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

    summary = fetch_exact_period_summary(
        "github.com/sg-evals/org-repo",
        "0" * 40,
        "1" * 40,
        "Python",
        period=8100,
        api_runner=fake_api,
    )

    assert summary["eligible_path_count"] == 65
    assert batch_sizes == [64, 1]


def test_unexpected_exact_path_pagination_fails_closed():
    def fake_api(graphql_query, **_variables):
        if "EraPanelChangedFiles" in graphql_query:
            return {
                "repository": {
                    "commit": {
                        "diff": {
                            "changedFiles": {
                                "nodes": [{"path": "a.py", "status": "MODIFIED"}],
                                "totalCount": 1,
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
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "unexpected",
                            },
                        }
                    }
                }
            }
        }

    try:
        fetch_exact_period_summary(
            "github.com/sg-evals/org-repo",
            "0" * 40,
            "1" * 40,
            "Python",
            period=8100,
            api_runner=fake_api,
        )
    except RuntimeError as error:
        assert "pagination" in str(error)
    else:
        raise AssertionError("unexpected exact-path pagination must fail closed")


def test_identical_boundaries_materialize_an_exact_empty_period():
    summary = fetch_exact_period_summary(
        "github.com/sg-evals/org-repo",
        "0" * 40,
        "0" * 40,
        "Python",
        period=8100,
        api_runner=lambda *_args, **_kwargs: pytest.fail("API must not be called"),
    )

    assert summary["eligible_path_count"] == 0
    assert summary["introduced_code_line_count"] == 0
