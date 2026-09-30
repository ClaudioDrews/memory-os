"""Unit tests for the #38 fixes: thinking-fallback in the Ollama client and
markdown-fence stripping before reflection JSON parsing.

The docker worker has no pytest infrastructure (the repo uses script-style
_test_*.py files), so this file adds one deliberately tiny pytest module
scoped to the two changed functions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm import _extract_response
from tasks.reflection import _strip_json_fences, reflect_on_memories


def test_extract_response_prefers_content():
    data = {"response": "hello", "reasoning": "chain", "thinking": "thoughts"}
    assert _extract_response(data) == "hello"


def test_extract_response_falls_back_to_reasoning():
    data = {"response": "", "reasoning": "chain-of-thought"}
    assert _extract_response(data) == "chain-of-thought"


def test_extract_response_falls_back_to_thinking():
    """glm-5.x-style models return chain-of-thought under `thinking`, with
    `response` empty — the case #38 reports on Ollama Cloud."""
    data = {"response": "", "thinking": "thoughts", "done_reason": "length"}
    assert _extract_response(data) == "thoughts"


def test_extract_response_empty_when_nothing_useful():
    assert _extract_response({}) == ""
    assert _extract_response({"response": "", "reasoning": None}) == ""


def test_strip_json_fences_passthrough_plain_json():
    body = '{"patterns": []}'
    assert _strip_json_fences(body) == '{"patterns": []}'


def test_strip_json_fences_language_tagged():
    body = '```json\n{"patterns": ["a"]}\n```'
    assert json.loads(_strip_json_fences(body)) == {"patterns": ["a"]}


def test_strip_json_fences_bare_fence_and_trailing_space():
    body = '```\n{"insights": ["x"]}\n```   '
    assert json.loads(_strip_json_fences(body)) == {"insights": ["x"]}


@pytest.mark.asyncio
async def test_reflection_parses_fenced_json_instead_of_raw_blob(monkeypatch):
    """Regression for #38 Bug 2: before the fix, a fenced response failed
    json.loads and the reflection was stored as {"raw": ...}, losing the
    structured fields."""
    captured = {}

    class FakeClient:
        async def scroll(self, **kwargs):
            class Point:
                id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                payload = {"source": "chat", "text": "user said hello"}
            return ([Point()], None)

        async def upsert(self, collection_name, points, **kwargs):
            captured["point"] = points[0]

        async def set_payload(self, **kwargs):
            return None

    async def fake_ollama(prompt):
        return '```json\n{"patterns": ["greetings"], "insights": ["hello"], "actions": [], "connections": []}\n```'

    async def fake_embedding(text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("tasks.reflection.ollama_chat", fake_ollama)
    monkeypatch.setattr("tasks.reflection.get_embedding", fake_embedding)

    await reflect_on_memories(FakeClient())

    payload = captured["point"].payload
    assert "patterns" in payload["text"], "reflection degraded to a raw blob"
    assert payload["source"] == "reflection"
