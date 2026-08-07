import copy
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from authorship.semantic_topology_deep_search import (
    DeepSearchClient,
    DeepSearchExecutionError,
    execute_plan,
    validate_inventory,
    validate_run_artifact,
)
from authorship.semantic_topology_protocol import canonical_sha256


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append(
            {"url": url, "payload": payload, "headers": headers, "timeout": timeout}
        )
        return self.responses.pop(0)


class DeepSearchClientTests(unittest.TestCase):
    def test_v7_create_and_poll_preserves_raw_conversation(self):
        transport = FakeTransport(
            [
                {
                    "conversation": {
                        "name": "users/~self/conversations/7",
                        "state": {"processing": {}},
                    }
                },
                {
                    "conversation": {
                        "name": "users/~self/conversations/7",
                        "state": {"completed": {}},
                        "questions": [
                            {
                                "answer": "Found evidence.",
                                "turns": [
                                    {
                                        "role": "assistant",
                                        "toolCalls": [
                                            {
                                                "name": "keyword_search",
                                                "input": {"query": "repo:x agent"},
                                            }
                                        ],
                                    }
                                ],
                                "sources": [
                                    {
                                        "url": "https://example.test/repo/-/blob/a.py?L1",
                                        "repository": "repo",
                                        "path": "a.py",
                                        "commit": "1" * 40,
                                    }
                                ],
                            }
                        ],
                    }
                },
            ]
        )
        client = DeepSearchClient(
            endpoint="https://sourcegraph.example",
            token="super-secret",
            transport=transport,
        )

        result = client.ask("Where is agent configuration?", poll_interval=0)

        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(result["conversation_identity"], "users/~self/conversations/7")
        create = transport.calls[0]
        self.assertTrue(
            create["url"].endswith("/api/deepsearch.v1.Service/CreateConversation")
        )
        question = create["payload"]["conversation"]["questions"][0]["input"][0][
            "question"
        ]["text"]
        self.assertEqual(question, "Where is agent configuration?")
        self.assertEqual(create["headers"]["Authorization"], "token super-secret")
        self.assertEqual(create["headers"]["User-Agent"], "agent-code-authorship/1.0")
        self.assertNotIn("super-secret", json.dumps(result))

    def test_terminal_api_error_is_returned_as_evidence_not_success(self):
        transport = FakeTransport(
            [
                {
                    "conversation": {
                        "name": "users/~self/conversations/8",
                        "state": {
                            "error": {
                                "code": "ERROR_ENTITLEMENT_EXCEEDED",
                                "message": "quota",
                            }
                        },
                    }
                }
            ]
        )
        client = DeepSearchClient("https://sourcegraph.example", "secret", transport)

        result = client.ask("question", poll_interval=0)

        self.assertEqual(result["terminal_status"], "error")
        self.assertEqual(result["error"]["code"], "ERROR_ENTITLEMENT_EXCEEDED")

    def test_processing_timeout_raises_explicit_execution_error(self):
        processing = {
            "conversation": {
                "name": "users/~self/conversations/9",
                "state": {"processing": {}},
            }
        }
        client = DeepSearchClient(
            "https://sourcegraph.example",
            "secret",
            FakeTransport([processing, processing]),
        )

        with self.assertRaisesRegex(DeepSearchExecutionError, "poll limit"):
            client.ask("question", poll_interval=0, max_polls=1)


class DeepSearchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "run_id": "sha256:" + "1" * 64,
            "canonical_repository_id": "example/repo",
            "prompt_family_id": "change_topology",
            "prompt_sha256": "2" * 64,
            "context_revision_kind": "cutoff",
            "prompt": "Explain topology in example/repo with cited evidence.",
            "status": "frozen",
        }
        self.plan = {
            "plan_version": 1,
            "plan_sha256": "3" * 64,
            "protocol_sha256": "4" * 64,
            "runs": [self.run],
        }

    def test_execute_plan_is_resumable_and_does_not_persist_token(self):
        class Client:
            def __init__(self):
                self.calls = 0
                self.token = "must-not-appear"

            def ask(self, prompt, **_kwargs):
                self.calls += 1
                return {
                    "terminal_status": "completed",
                    "conversation_identity": "users/~self/conversations/1",
                    "raw_conversation": {
                        "name": "users/~self/conversations/1",
                        "state": {"completed": {}},
                        "questions": [{"answer": "answer", "sources": []}],
                    },
                    "error": None,
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            inventory = execute_plan(self.plan, output, client)
            reused = execute_plan(self.plan, output, client)

            self.assertEqual(client.calls, 1)
            self.assertEqual(inventory, reused)
            self.assertEqual(inventory["counts"], {"completed": 1})
            artifact = json.loads(next((output / "runs").glob("*.json")).read_text())
            self.assertEqual(validate_run_artifact(artifact, self.plan), [])
            root = Path(__file__).resolve().parents[1]
            run_schema = json.loads(
                (
                    root / "study/semantic-topology-deep-search-run.schema.json"
                ).read_text()
            )
            inventory_schema = json.loads(
                (
                    root / "study/semantic-topology-deep-search-inventory.schema.json"
                ).read_text()
            )
            jsonschema.Draft202012Validator(run_schema).validate(artifact)
            jsonschema.Draft202012Validator(inventory_schema).validate(inventory)
            self.assertNotIn("must-not-appear", json.dumps(artifact))
            self.assertEqual(validate_inventory(inventory, self.plan, output), [])

    def test_rebuild_derived_reextracts_without_api_call(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def ask(self, prompt, **_kwargs):
                self.calls += 1
                return {
                    "terminal_status": "completed",
                    "conversation_identity": "users/~self/conversations/1",
                    "raw_conversation": {
                        "state": {"completed": {}},
                        "questions": [
                            {
                                "answer": [
                                    {
                                        "markdown": {
                                            "text": (
                                                "[code](https://sg.test/r/repo@"
                                                + "a" * 40
                                                + "/-/blob/a.py?L1)\n\n"
                                                "Deterministic query:\n"
                                                "```\nrepo:^repo$ thing\n```"
                                            )
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                    "error": None,
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            execute_plan(self.plan, output, client)
            inventory = execute_plan(self.plan, output, client, rebuild_derived=True)
            artifact = json.loads(
                (output / inventory["runs"][0]["artifact_path"]).read_text()
            )

        self.assertEqual(client.calls, 1)
        self.assertTrue(artifact["raw_answer"])
        self.assertEqual(artifact["proposed_queries"], ["repo:^repo$ thing"])
        self.assertEqual(len(artifact["cited_files"]), 1)

    def test_invalid_reused_artifact_is_reexecuted(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def ask(self, prompt, **_kwargs):
                self.calls += 1
                return {
                    "terminal_status": "error",
                    "conversation_identity": None,
                    "raw_conversation": {},
                    "error": {"code": "transport_error", "message": "unavailable"},
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            execute_plan(self.plan, output, client)
            path = next((output / "runs").glob("*.json"))
            broken = json.loads(path.read_text())
            broken["prompt_sha256"] = "f" * 64
            path.write_text(json.dumps(broken))

            inventory = execute_plan(self.plan, output, client)

            self.assertEqual(client.calls, 2)
            self.assertEqual(inventory["counts"], {"error": 1})

    def test_valid_terminal_error_retries_only_when_explicitly_requested(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def ask(self, prompt, **_kwargs):
                self.calls += 1
                status = "error" if self.calls == 1 else "completed"
                return {
                    "terminal_status": status,
                    "conversation_identity": None,
                    "raw_conversation": {},
                    "error": (
                        {"code": "transport_error", "message": "temporary"}
                        if status == "error"
                        else None
                    ),
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            execute_plan(self.plan, output, client)
            execute_plan(self.plan, output, client)
            self.assertEqual(client.calls, 1)

            inventory = execute_plan(
                self.plan, output, client, retry_terminal={"error"}
            )
            artifact = json.loads(next((output / "runs").glob("*.json")).read_text())

        self.assertEqual(client.calls, 2)
        self.assertEqual(inventory["counts"], {"completed": 1})
        self.assertEqual(len(artifact["attempt_history"]), 1)
        self.assertEqual(
            artifact["attempt_history"][0]["error"]["code"], "transport_error"
        )
        self.assertRegex(
            artifact["attempt_history"][0]["artifact_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_retry_can_be_restricted_to_error_code(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def ask(self, prompt, **_kwargs):
                self.calls += 1
                return {
                    "terminal_status": "error",
                    "conversation_identity": None,
                    "raw_conversation": {},
                    "error": {"code": "transport_error", "message": "temporary"},
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            execute_plan(self.plan, output, client)
            execute_plan(
                self.plan,
                output,
                client,
                retry_terminal={"error"},
                retry_error_codes={"ERROR_INTERNAL"},
            )

        self.assertEqual(client.calls, 1)

    def test_pending_context_revision_becomes_explicit_failure_without_api_call(self):
        plan = copy.deepcopy(self.plan)
        plan["runs"][0]["status"] = "pending_context_revision"

        class Client:
            def ask(self, *_args, **_kwargs):
                raise AssertionError("API must not be called")

        with tempfile.TemporaryDirectory() as directory:
            inventory = execute_plan(plan, Path(directory), Client())
            artifact = json.loads(
                next((Path(directory) / "runs").glob("*.json")).read_text()
            )

        self.assertEqual(inventory["counts"], {"unavailable": 1})
        self.assertEqual(artifact["error"]["code"], "context_revision_unresolved")

    def test_shards_execute_disjoint_plan_positions(self):
        plan = copy.deepcopy(self.plan)
        second = copy.deepcopy(self.run)
        second["run_id"] = "sha256:" + "9" * 64
        second["prompt_sha256"] = "8" * 64
        second["prompt_family_id"] = "review_validation"
        plan["runs"].append(second)

        class Client:
            def __init__(self):
                self.prompts = []

            def ask(self, prompt, **_kwargs):
                self.prompts.append(prompt)
                return {
                    "terminal_status": "completed",
                    "conversation_identity": "conversation",
                    "raw_conversation": {"state": {"completed": {}}},
                    "error": None,
                }

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = execute_plan(plan, output, client, shard_index=0, shard_count=2)
            second_inventory = execute_plan(
                plan, output, client, shard_index=1, shard_count=2
            )

        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(first["materialized_run_count"], 1)
        self.assertEqual(second_inventory["materialized_run_count"], 2)

    def test_invalid_shard_configuration_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "shard_index"):
                execute_plan(
                    self.plan,
                    Path(directory),
                    object(),
                    shard_index=2,
                    shard_count=2,
                )

    def test_run_checksum_tampering_fails_validation(self):
        artifact = {
            "artifact_version": 1,
            "run_id": self.run["run_id"],
            "plan_sha256": self.plan["plan_sha256"],
            "protocol_sha256": self.plan["protocol_sha256"],
            "canonical_repository_id": "example/repo",
            "prompt_family_id": "change_topology",
            "prompt_sha256": self.run["prompt_sha256"],
            "prompt": self.run["prompt"],
            "terminal_status": "completed",
            "conversation_identity": "users/~self/conversations/1",
            "search_context": {"repository": "example/repo"},
            "searches": [],
            "cited_files": [],
            "cited_commits": [],
            "model_metadata": {
                "status": "unavailable",
                "reason": "not_exposed_by_api",
            },
            "raw_answer": "answer",
            "raw_conversation": {},
            "error": None,
        }
        artifact["artifact_sha256"] = canonical_sha256(artifact)
        artifact["raw_answer"] = "tampered"

        errors = validate_run_artifact(artifact, self.plan)

        self.assertIn("artifact_sha256 mismatch", errors)


if __name__ == "__main__":
    unittest.main()
