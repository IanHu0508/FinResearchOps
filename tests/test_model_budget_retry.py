"""A single explicit truncation recovery never resets financial or call limits."""

from copy import deepcopy
from decimal import Decimal
import unittest

from finauditgate.adapters.model_budget import ModelBudget


class ModelBudgetRetryTest(unittest.TestCase):
    def test_one_explicit_retry_keeps_both_calls_and_all_usage(self):
        budget = ModelBudget(ceiling_cny=None, max_output_tokens=10)
        self.assertFalse(budget.allow_truncated_retry())
        self.assertEqual(1, budget.reserve("first"))
        first = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        self.assertFalse(budget.record_usage(first, truncated=True))
        before = deepcopy(budget.receipt())
        self.assertTrue(budget.allow_truncated_retry())
        self.assertEqual(before, budget.receipt())
        self.assertEqual(2, budget.reserve("second"))
        second = {"input_tokens": 25, "output_tokens": 10, "total_tokens": 35}
        self.assertFalse(budget.record_usage(second, truncated=True))
        self.assertFalse(budget.allow_truncated_retry())
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            budget.reserve("third")
        receipt = budget.receipt()
        self.assertEqual(2, receipt["calls"])
        self.assertEqual([first, second], receipt["usage"])
        self.assertEqual(Decimal("0.000945"), Decimal(receipt["uncached_price_estimate_cny"]))
        self.assertGreaterEqual(Decimal(receipt["reserved_upper_cny"]), Decimal(before["reserved_upper_cny"]))
        self.assertEqual(10, budget.max_output_tokens)

    def test_output_limit_violation_is_never_unlocked(self):
        budget = ModelBudget(max_output_tokens=10)
        budget.reserve("first")
        budget.record_usage({"input_tokens": 1, "output_tokens": 11, "total_tokens": 12}, truncated=True)
        before = deepcopy(budget.receipt())
        self.assertFalse(budget.allow_truncated_retry())
        self.assertEqual(before, budget.receipt())
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_LIMIT_VIOLATION"):
            budget.reserve("second")

    def test_retry_is_still_subject_to_original_call_limit(self):
        budget = ModelBudget(max_calls=1, max_output_tokens=10)
        budget.reserve("first")
        budget.record_usage({"input_tokens": 1, "output_tokens": 10}, truncated=True)
        before = deepcopy(budget.receipt())
        self.assertTrue(budget.allow_truncated_retry())
        with self.assertRaisesRegex(ValueError, "MODEL_CALL_LIMIT"):
            budget.reserve("second")
        self.assertEqual(before, budget.receipt())
        self.assertFalse(budget.allow_truncated_retry())

    def test_retry_is_still_subject_to_original_spend_limit(self):
        budget = ModelBudget(ceiling_cny="15", input_per_million="0",
            output_per_million="1000000", max_output_tokens=10)
        budget.reserve("first")
        budget.record_usage({"input_tokens": 1, "output_tokens": 10}, truncated=True)
        before = deepcopy(budget.receipt())
        self.assertTrue(budget.allow_truncated_retry())
        with self.assertRaisesRegex(ValueError, "MODEL_SPEND_LIMIT"):
            budget.reserve("second")
        self.assertEqual(before, budget.receipt())
        self.assertEqual("10", budget.receipt()["uncached_price_estimate_cny"])

    def test_retry_keeps_input_limit_and_unknown_usage(self):
        budget = ModelBudget(max_input_bytes=5, max_output_tokens=10)
        budget.reserve("first")
        budget.record_usage({}, truncated=True)
        before = deepcopy(budget.receipt())
        self.assertTrue(budget.allow_truncated_retry())
        with self.assertRaisesRegex(ValueError, "MODEL_INPUT_LIMIT"):
            budget.reserve("longer")
        self.assertEqual(before, budget.receipt())
        self.assertEqual([{}], budget.receipt()["usage"])
        self.assertIsNone(budget.receipt()["uncached_price_estimate_cny"])

    def test_successful_retry_does_not_grant_a_later_second_retry(self):
        budget = ModelBudget(max_output_tokens=10)
        budget.reserve("first")
        budget.record_usage({"input_tokens": 1, "output_tokens": 10}, truncated=True)
        self.assertTrue(budget.allow_truncated_retry())
        budget.reserve("second")
        self.assertTrue(budget.record_usage({"input_tokens": 1, "output_tokens": 5}))
        budget.reserve("later independent call")
        budget.record_usage({"input_tokens": 1, "output_tokens": 10}, truncated=True)
        self.assertFalse(budget.allow_truncated_retry())
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            budget.reserve("extra retry")


if __name__ == "__main__":
    unittest.main()
