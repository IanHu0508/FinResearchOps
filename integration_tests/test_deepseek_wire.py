import json
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from tradingagents.llm_clients import create_llm_client
from finauditgate.adapters.model_http import model_http_client


class DeepSeekWireTest(unittest.TestCase):
    def test_new_client_for_update_does_not_overwrite_prior_wire_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for start in (0, 3):
                with model_http_client("deepseek", 64, trace_root=root, start_index=start,
                     transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
                    client.post("https://api.deepseek.com/chat/completions", json={"model":"synthetic", "messages":[], "max_tokens":64})
            self.assertEqual(["wire-001.json", "wire-004.json"], sorted(p.name for p in root.iterdir()))

    def test_actual_sdk_sends_documented_deepseek_token_limit(self):
        captured = []
        def handle(request):
            data = json.loads(request.content)
            captured.append(data)
            self.assertEqual(len(request.content), int(request.headers["content-length"]))
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
                "model": "deepseek-v4-pro", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-offline-key"}), \
                model_http_client("deepseek", 64, transport=httpx.MockTransport(handle), reasoning_effort="low") as http:
            llm = create_llm_client("deepseek", "deepseek-v4-pro", max_tokens=64,
                                   max_retries=0, http_client=http).get_llm()
            llm.invoke("Reply OK.")
            llm.bind(reasoning_effort="high").invoke("Reply OK.")
        self.assertEqual(64, captured[0].get("max_tokens"))
        self.assertNotIn("max_completion_tokens", captured[0])
        self.assertEqual("low", captured[0].get("reasoning_effort"))
        self.assertEqual("high", captured[1].get("reasoning_effort"))
        self.assertEqual(64, captured[1].get("max_tokens"))
