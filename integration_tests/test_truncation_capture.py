"""Keep interrupted SDK responses as failure evidence, never completed work."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import BaseModel
from tradingagents.llm_clients import create_llm_client

from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.model_http import model_http_client
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.tradingagents_native import _capture_handler


def completion(*, content='{"value":', usage=True):
    body = {"id": "synthetic-truncated", "object": "chat.completion", "created": 0,
        "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "length",
            "message": {"role": "assistant", "content": content,
                        "reasoning_content": "SYNTHETIC_PARTIAL_REASONING"}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 20, "completion_tokens": 64, "total_tokens": 84,
            "completion_tokens_details": {"reasoning_tokens": 62},
            "prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 13}
    return ChatCompletion.model_validate(body)


class TruncationCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.traces = self.root / "model-traces"
        self.traces.mkdir()
        self.budget = ModelBudget(ceiling_cny=None, max_output_tokens=64)
        self.capture = _capture_handler(self.budget, self.traces)

    def start(self, run_id="failed", node="Portfolio Manager"):
        self.capture.on_chat_model_start({}, [[HumanMessage(content="SYNTHETIC_REQUEST")]],
            run_id=run_id, metadata={"langgraph_node": node})

    def fail(self, *, value=None, run_id="failed"):
        error = LengthFinishReasonError(completion=value or completion())
        # These are deliberately outside the response body and must not leak.
        error.client = object()
        error.headers = {"authorization": "SYNTHETIC_SECRET_NOT_RESPONSE_DATA"}
        self.capture.on_llm_error(error, run_id=run_id)
        return error

    def test_sdk_length_failure_preserves_body_usage_and_blocks_budget(self):
        self.start()
        original = completion()
        self.fail(value=original)
        record = json.loads((self.traces / "call-001-failure-response.json").read_text())
        self.assertEqual("finresearchops.native-failure-response/v1", record["schema_version"])
        self.assertEqual(original.model_dump(mode="json"), record["failure_response"]["provider_response"])
        failure = record["failure_response"]
        self.assertEqual(["length"], failure["finish_reasons"])
        self.assertEqual(62, failure["provider_usage"]["completion_tokens_details"]["reasoning_tokens"])
        self.assertEqual([len('{"value":')], failure["content_characters"])
        self.assertEqual([len("SYNTHETIC_PARTIAL_REASONING")], failure["reasoning_characters"])
        self.assertEqual({"input_tokens": 20, "output_tokens": 64, "total_tokens": 84}, failure["usage"])
        self.assertTrue(failure["truncated"])
        self.assertEqual([failure["usage"]], self.budget.receipt()["usage"])
        self.assertIsNotNone(self.budget.receipt()["uncached_price_estimate_cny"])
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            self.budget.reserve("No automatic retry.")
        row = self.capture.model_calls[0]
        self.assertEqual("LengthFinishReasonError", row["error_type"])
        self.assertEqual(failure, row["failure_response"])
        self.assertNotIn("output", row)
        self.assertNotIn("SYNTHETIC_SECRET_NOT_RESPONSE_DATA", json.dumps(record))
        self.assertNotIn("client", record["failure_response"])

    def test_missing_provider_usage_remains_unknown(self):
        self.start()
        self.fail(value=completion(usage=False))
        failure = self.capture.model_calls[0]["failure_response"]
        self.assertIsNone(failure["provider_usage"])
        self.assertIsNone(failure["usage"])
        self.assertEqual([{}], self.budget.receipt()["usage"])
        self.assertIsNone(self.budget.receipt()["uncached_price_estimate_cny"])
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            self.budget.reserve("Do not silently turn unknown usage into zero.")

    def test_repeated_error_callback_does_not_charge_the_same_response_twice(self):
        self.start()
        error = self.fail()
        saved = (self.traces / "call-001-failure-response.json").read_bytes()
        self.capture.on_llm_error(error, run_id="failed")
        self.assertEqual(1, len(self.budget.receipt()["usage"]))
        self.assertEqual(saved, (self.traces / "call-001-failure-response.json").read_bytes())

    def test_other_errors_only_keep_error_type(self):
        self.start()
        error = RuntimeError("SYNTHETIC_ERROR_MESSAGE_NOT_FOR_PERSISTENCE")
        error.completion = {"headers": {"authorization": "SYNTHETIC_SECRET"}}
        self.capture.on_llm_error(error, run_id="failed")
        self.assertEqual("RuntimeError", self.capture.model_calls[0]["error_type"])
        self.assertNotIn("failure_response", self.capture.model_calls[0])
        self.assertEqual([], self.budget.receipt()["usage"])
        self.assertEqual(["call-001-request.json"], [p.name for p in self.traces.iterdir()])

    def test_failure_content_that_is_complete_json_is_not_reusable(self):
        value = {"claims": [{"statement": "SYNTHETIC_CLAIM", "business_mechanism": "Synthetic mechanism.",
            "evidence_refs": ["S01"], "would_change_mind": "Synthetic observation.",
            "uncertainty": "Synthetic uncertainty."} for _ in range(2)]}
        self.start("ok", "Bull Researcher")
        message = AIMessage(content=json.dumps(value), usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"finish_reason": "stop"}, id="synthetic-completed")
        self.capture.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id="ok")
        self.start("failed", "Bear Researcher")
        self.fail(value=completion(content=json.dumps(value)))
        request, sources = {"symbol": "SYNTHETIC"}, {"sources": []}
        (self.root / "request.json").write_text(json.dumps({"request": request, "sources": sources}))
        (self.root / "runtime-receipt.json").write_text(json.dumps({
            "schema_version": "finresearchops.thesis-runtime/v1", "budget": self.budget.receipt(),
            "model_calls": self.capture.model_calls}))
        recovered = CompletedCalls(self.root, request, sources, "deepseek-flash")
        self.assertEqual(1, len(recovered.rows))
        self.assertEqual("synthetic-completed", recovered.rows[0]["output"][0]["id"])
        self.assertNotIn("output", self.capture.model_calls[1])
        self.assertEqual(value, json.loads(self.capture.model_calls[1]["failure_response"][
            "provider_response"]["choices"][0]["message"]["content"]))

    def test_actual_sdk_error_still_propagates_after_failure_capture(self):
        class Answer(BaseModel):
            value: str

        def respond(request):
            return httpx.Response(200, json=completion().model_dump(mode="json"))

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-offline-key"}), \
                model_http_client("deepseek", 64, transport=httpx.MockTransport(respond)) as http:
            llm = create_llm_client("deepseek", "deepseek-flash", max_tokens=64,
                max_retries=0, http_client=http).get_llm()
            with self.assertRaises(LengthFinishReasonError):
                llm.with_structured_output(Answer, method="json_mode", include_raw=True).invoke(
                    "Return JSON with a value field.", config={"callbacks": [self.capture],
                    "metadata": {"langgraph_node": "Portfolio Manager"}})
        self.assertEqual("LengthFinishReasonError", self.capture.model_calls[0]["error_type"])
        self.assertEqual(64, self.budget.receipt()["usage"][0]["output_tokens"])
        self.assertTrue((self.traces / "call-001-failure-response.json").exists())

    def test_recording_failure_does_not_replace_the_original_sdk_error(self):
        self.start()
        original = LengthFinishReasonError(completion=completion())
        with patch("finauditgate.adapters.tradingagents_native.write_once", side_effect=OSError("SYNTHETIC_DISK_ERROR")):
            # This is the SDK caller's callback/error propagation pattern.
            with self.assertRaises(LengthFinishReasonError) as raised:
                try:
                    raise original
                except LengthFinishReasonError as error:
                    self.capture.on_llm_error(error, run_id="failed")
                    raise
        self.assertIs(original, raised.exception)
        row = self.capture.model_calls[0]
        self.assertEqual("LengthFinishReasonError", row["error_type"])
        self.assertEqual("OSError", row["failure_capture_error_type"])
        self.assertNotIn("output", row)
        self.assertEqual(64, self.budget.receipt()["usage"][0]["output_tokens"])


if __name__ == "__main__":
    unittest.main()
