"""Tests for provider-agnostic query embeddings and visible degradation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(__file__))

from scripts import context_enhancer as ce
from icarus import hooks


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}


class LocalEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "EMBEDDING_API_BASE": ce.EMBEDDING_API_BASE,
            "EMBEDDING_API_KEY": ce.EMBEDDING_API_KEY,
            "EMBEDDING_MODEL": ce.EMBEDDING_MODEL,
            "REQUEST_TIMEOUT": ce.REQUEST_TIMEOUT,
            "EMBEDDING_REQUEST_RETRIES": ce.EMBEDDING_REQUEST_RETRIES,
            "SPARSE_QUERY_ENABLED": ce.SPARSE_QUERY_ENABLED,
        }

    def tearDown(self):
        for name, value in self.config.items():
            setattr(ce, name, value)

    @mock.patch.object(ce.requests, "post", return_value=_Response())
    def test_custom_openai_endpoint_and_api_key(self, post):
        ce.EMBEDDING_API_BASE = "http://localai.lan/v1"
        ce.EMBEDDING_API_KEY = "local-secret"
        ce.EMBEDDING_MODEL = "memory-embedding"
        ce.REQUEST_TIMEOUT = 30
        ce.EMBEDDING_REQUEST_RETRIES = 1

        result = ce.embed_query_with_status("wiki query")

        self.assertEqual(result.vector, [0.1, 0.2, 0.3])
        self.assertIsNone(result.error)
        self.assertEqual(post.call_args.args[0], "http://localai.lan/v1/embeddings")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer local-secret",
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 30)

    @mock.patch.object(ce.time, "sleep")
    @mock.patch.object(ce.requests, "post", side_effect=requests.Timeout("cold start"))
    def test_timeout_retries_and_returns_stable_error(self, post, sleep):
        ce.EMBEDDING_API_BASE = "http://localai.lan/v1"
        ce.EMBEDDING_API_KEY = "local-secret"
        ce.EMBEDDING_REQUEST_RETRIES = 1

        result = ce.embed_query_with_status("wiki query")

        self.assertIsNone(result.vector)
        self.assertEqual(result.error, "embedding_timeout")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()

    @mock.patch.object(ce.subprocess, "run")
    def test_sparse_query_can_be_disabled_without_subprocess(self, run):
        ce.SPARSE_QUERY_ENABLED = False

        self.assertIsNone(ce.embed_query_sparse("wiki query"))
        run.assert_not_called()

    def test_context_enhancer_loads_from_mounted_hermes_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "scripts" / "context_enhancer.py"
            helper.parent.mkdir()
            helper.write_text("MOUNTED = True\n", encoding="utf-8")
            sys.modules.pop("icarus_context_enhancer", None)

            with (
                mock.patch.object(
                    hooks.importlib,
                    "import_module",
                    side_effect=ModuleNotFoundError(
                        "No module named 'scripts'", name="scripts"
                    ),
                ),
                mock.patch.object(hooks.state, "HERMES_HOME", Path(tmp)),
            ):
                loaded = hooks._load_context_enhancer()

            self.assertTrue(loaded.MOUNTED)
            sys.modules.pop("icarus_context_enhancer", None)

    def test_pre_llm_hook_requires_warning_before_answer(self):
        old_tokens = hooks._last_query_tokens
        hooks._last_query_tokens = set()
        try:
            with (
                mock.patch.object(hooks.state, "recall", return_value=[]),
                mock.patch.object(
                    hooks,
                    "_search_qdrant_with_status",
                    return_value=([], "embedding_timeout"),
                ),
                mock.patch.object(hooks, "_search_sessions", return_value=[]),
                mock.patch.object(hooks, "_search_facts", return_value=[]),
            ):
                result = hooks.pre_llm_call(
                    session_id="test",
                    user_message="find the wiki entry about backups",
                    is_first_turn=True,
                )
        finally:
            hooks._last_query_tokens = old_tokens

        self.assertIsNotNone(result)
        context = result["context"]
        self.assertTrue(context.startswith("[memory-retrieval-warning]"))
        self.assertIn("Before any other response text", context)
        self.assertIn("embedding_timeout", context)


if __name__ == "__main__":
    unittest.main()
