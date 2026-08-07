import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from authorship import sg
from authorship.sg import SOURCEGRAPH_API_TIMEOUT_SECONDS, _src_environment, api


class SourcegraphEnvironmentTests(unittest.TestCase):
    def test_maps_connected_mcp_environment_to_src_cli(self):
        with patch.dict(
            os.environ,
            {
                "SOURCEGRAPH_MCP_URL": "https://example.sourcegraph.com/.api/mcp/all",
                "SOURCEGRAPH_ACCESS_TOKEN": "secret",
            },
            clear=True,
        ):
            environment = _src_environment()

        self.assertEqual(environment["SRC_ENDPOINT"], "https://example.sourcegraph.com")
        self.assertEqual(environment["SRC_ACCESS_TOKEN"], "secret")

    def test_explicit_src_configuration_wins(self):
        with patch.dict(
            os.environ,
            {
                "SRC_ENDPOINT": "https://explicit.example",
                "SRC_ACCESS_TOKEN": "explicit",
                "SOURCEGRAPH_MCP_URL": "https://ignored.example/.api/mcp/all",
                "SOURCEGRAPH_ACCESS_TOKEN": "ignored",
            },
            clear=True,
        ):
            environment = _src_environment()

        self.assertEqual(environment["SRC_ENDPOINT"], "https://explicit.example")
        self.assertEqual(environment["SRC_ACCESS_TOKEN"], "explicit")

    @patch("authorship.sg.subprocess.run")
    def test_api_limits_src_cli_runtime(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["src"], 0, '{"data":{"repository":null}}', ""
        )

        api('{ repository(name: "example") { name } }')

        self.assertEqual(
            run.call_args.kwargs["timeout"], SOURCEGRAPH_API_TIMEOUT_SECONDS
        )
        self.assertGreater(SOURCEGRAPH_API_TIMEOUT_SECONDS, 60)

    @patch("authorship.sg.subprocess.run")
    def test_api_allows_graphql_variable_named_query(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["src"], 0, '{"data":{"search":{"results":{"resultCount":0}}}}', ""
        )

        api(
            "query Search($query:String!){search(query:$query){results{resultCount}}}",
            query="repo:^example$",
        )

        self.assertIn("query=repo:^example$", run.call_args.args[0])

    @patch("authorship.sg.subprocess.run")
    def test_api_serializes_list_variables_as_json(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["src"], 0, '{"data":{"repository":null}}', ""
        )

        api(
            'query Paths($paths:[String!]!){repository(name:"x"){name}}',
            paths=["src/a.py", "tests/b.py"],
        )

        command = run.call_args.args[0]
        index = command.index("-vars")
        self.assertEqual(
            command[index + 1],
            '{"paths":["src/a.py","tests/b.py"]}',
        )

    @patch("authorship.sg.subprocess.run")
    def test_api_reports_src_cli_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired(["src", "api"], 90)

        with self.assertRaisesRegex(RuntimeError, "timed out after 90 seconds"):
            api("{ currentUser { username } }")

    @patch("authorship.sg.subprocess.run")
    def test_api_fails_closed_on_cli_json_and_graphql_errors(self, run):
        run.return_value = subprocess.CompletedProcess(["src"], 1, "", "denied")
        with self.assertRaisesRegex(RuntimeError, "denied"):
            api("{ currentUser { username } }")

        run.return_value = subprocess.CompletedProcess(["src"], 0, "{", "")
        with self.assertRaisesRegex(RuntimeError, "unparseable"):
            api("{ currentUser { username } }")

        run.return_value = subprocess.CompletedProcess(
            ["src"], 0, '{"errors":[{"message":"bad"}]}', ""
        )
        with self.assertRaisesRegex(RuntimeError, "GraphQL errors"):
            api("{ currentUser { username } }")

    def test_authentication_requires_a_username(self):
        with patch(
            "authorship.sg.api", return_value={"currentUser": {"username": "u"}}
        ):
            self.assertEqual(sg.check_auth(), "u")
        with patch("authorship.sg.api", return_value={"currentUser": None}):
            with self.assertRaisesRegex(SystemExit, "not authenticated"):
                sg.check_auth()

    def test_cloned_fails_closed_on_api_errors(self):
        with patch(
            "authorship.sg.api",
            return_value={"repository": {"mirrorInfo": {"cloned": True}}},
        ):
            self.assertTrue(sg.cloned("github.com/org/repo"))
        with patch("authorship.sg.api", side_effect=RuntimeError("offline")):
            self.assertFalse(sg.cloned("github.com/org/repo"))

    def test_fork_map_and_resolution_are_deterministic(self):
        fixture = "github.com/sg-evals/org-repo\torg/repo\n\n"
        with patch.object(Path, "read_text", return_value=fixture):
            self.assertEqual(
                sg.fork_map(), {"org/repo": "github.com/sg-evals/org-repo"}
            )

        with patch("authorship.sg.cloned", side_effect=[True]):
            self.assertEqual(sg.resolve("org/repo", {}), "github.com/org/repo")
        with patch("authorship.sg.cloned", side_effect=[False, True]):
            self.assertEqual(
                sg.resolve("org/repo", {"org/repo": "github.com/sg-evals/org-repo"}),
                "github.com/sg-evals/org-repo",
            )
        with patch("authorship.sg.cloned", side_effect=[False]):
            self.assertIsNone(sg.resolve("org/repo", {}))

    def test_code_files_filters_noncode_and_skipped_paths(self):
        response = {
            "repository": {
                "commit": {
                    "tree": {
                        "files": [
                            {"path": "src/a.py"},
                            {"path": "README.md"},
                            {"path": "vendor/b.py"},
                        ]
                    }
                }
            }
        }
        with patch("authorship.sg.api", return_value=response):
            self.assertEqual(sg.code_files("github.com/org/repo"), ["src/a.py"])

    def test_stride_sample_handles_small_and_large_inputs(self):
        values = ["a", "b", "c", "d"]
        self.assertIs(sg.stride_sample(values, 4), values)
        self.assertEqual(sg.stride_sample(values, 2), ["a", "c"])

    def test_blob_and_blame_handles_present_and_missing_blobs(self):
        present = {"repository": {"commit": {"blob": {"content": "x", "blame": []}}}}
        with patch("authorship.sg.api", return_value=present):
            self.assertEqual(
                sg.blob_and_blame("github.com/org/repo", "a.py")["content"], "x"
            )
        with patch("authorship.sg.api", return_value={"repository": None}):
            self.assertEqual(sg.blob_and_blame("github.com/org/repo", "missing.py"), {})


if __name__ == "__main__":
    unittest.main()
