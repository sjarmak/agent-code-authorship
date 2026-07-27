import os
import unittest
from unittest.mock import patch

from authorship.sg import _src_environment


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


if __name__ == "__main__":
    unittest.main()
