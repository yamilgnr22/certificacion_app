from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from llm import LLMProviderError
from llm.provider import OpenAIProvider


class _FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents[len(self.calls) - 1]
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_openai_factory(contents):
    completions = _FakeCompletions(contents)

    class _FakeOpenAI:
        def __init__(self, api_key=None):
            self.chat = SimpleNamespace(completions=completions)

    return _FakeOpenAI, completions


class OpenAIProviderRetryTest(unittest.TestCase):
    """F3-T2: un reintento estructurado ante JSON invalido del LLM."""

    def test_invalid_json_then_valid_retries_once(self):
        fake_cls, completions = _fake_openai_factory([
            "esto no es json",
            '{"intent": "question", "args": {}}',
        ])
        provider = OpenAIProvider(api_key="test-key")
        with patch("openai.OpenAI", fake_cls):
            data = provider.complete_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(data["intent"], "question")
        self.assertEqual(provider.last_retries, 1)
        self.assertEqual(len(completions.calls), 2)
        # El reintento incluye la respuesta fallida y la correccion.
        retry_messages = completions.calls[1]["messages"]
        self.assertEqual(len(retry_messages), 4)
        self.assertEqual(retry_messages[2]["role"], "assistant")
        self.assertIn("no fue valida", retry_messages[3]["content"])

    def test_valid_json_first_try_reports_zero_retries(self):
        fake_cls, completions = _fake_openai_factory(['{"intent": "navigate", "args": {}}'])
        provider = OpenAIProvider(api_key="test-key")
        with patch("openai.OpenAI", fake_cls):
            data = provider.complete_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(data["intent"], "navigate")
        self.assertEqual(provider.last_retries, 0)
        self.assertEqual(len(completions.calls), 1)

    def test_two_invalid_responses_raise_provider_error(self):
        fake_cls, completions = _fake_openai_factory(["basura", "[1, 2, 3]"])
        provider = OpenAIProvider(api_key="test-key")
        with patch("openai.OpenAI", fake_cls):
            with self.assertRaises(LLMProviderError):
                provider.complete_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(len(completions.calls), 2)


if __name__ == "__main__":
    unittest.main()
