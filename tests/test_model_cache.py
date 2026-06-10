from __future__ import annotations

import unittest

import model_cache
from model_cache import cached_build_financial_model, clear_model_cache
from tests.test_financial_model import sample_payload


class ModelCacheTest(unittest.TestCase):
    """F3-T3: payloads identicos no deben reconstruir el modelo."""

    def setUp(self):
        clear_model_cache()
        self.calls = 0
        self._real = model_cache.build_financial_model

        def counting(payload):
            self.calls += 1
            return self._real(payload)

        model_cache.build_financial_model = counting

    def tearDown(self):
        model_cache.build_financial_model = self._real
        clear_model_cache()

    def test_same_payload_builds_once_and_shares_result(self):
        first = cached_build_financial_model(sample_payload())
        second = cached_build_financial_model(sample_payload())

        self.assertIs(first, second)
        self.assertEqual(self.calls, 1)

    def test_different_payload_builds_again(self):
        cached_build_financial_model(sample_payload())
        other = sample_payload()
        other["income"]["base_income_usd"] = 120000

        cached_build_financial_model(other)

        self.assertEqual(self.calls, 2)

    def test_lru_evicts_oldest_entry(self):
        first = sample_payload()
        cached_build_financial_model(first)
        for index in range(model_cache.MAX_ENTRIES):
            payload = sample_payload()
            payload["income"]["base_income_usd"] = 100000 + index + 1
            cached_build_financial_model(payload)

        calls_before = self.calls
        cached_build_financial_model(first)

        self.assertEqual(self.calls, calls_before + 1)


if __name__ == "__main__":
    unittest.main()
