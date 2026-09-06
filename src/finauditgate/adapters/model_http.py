"""Provider limits enforced on the actual wire, after SDK field rewriting."""

import json

from finauditgate.core.artifacts import canonical_json_bytes, write_once


def model_http_client(provider, max_output_tokens, *, trace_root=None, transport=None, reasoning_effort=None):
    import httpx
    if provider != "deepseek":
        return httpx.Client(timeout=120, transport=transport)
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 16384:
        raise ValueError("MODEL_HTTP_OUTPUT_LIMIT_INVALID")
    if reasoning_effort not in (None, "low", "high", "max"):
        raise ValueError("MODEL_REASONING_EFFORT_INVALID")

    class DeepSeekLimitTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = transport or httpx.HTTPTransport(retries=0)
            self.count = 0

        def handle_request(self, request):
            if (request.url.scheme != "https" or request.url.host != "api.deepseek.com"
                    or request.url.path not in ("/chat/completions", "/v1/chat/completions")):
                raise ValueError("UNEXPECTED_DEEPSEEK_ENDPOINT")
            payload = json.loads(request.read())
            limits = [payload.get(key) for key in ("max_tokens", "max_completion_tokens")
                      if payload.get(key) is not None]
            if any(type(x) is not int or x <= 0 for x in limits):
                raise ValueError("MODEL_HTTP_OUTPUT_LIMIT_INVALID")
            # langchain-openai renames max_tokens for OpenAI. DeepSeek's
            # documented Chat Completions field is still max_tokens.
            removed_alias = "max_completion_tokens" in payload
            payload.pop("max_completion_tokens", None)
            payload["max_tokens"] = min([max_output_tokens, *limits])
            if reasoning_effort is not None:
                if payload.get("reasoning_effort") is None:
                    payload["reasoning_effort"] = reasoning_effort
            if payload.get("reasoning_effort") not in (None, "low", "high", "max"):
                raise ValueError("MODEL_REASONING_EFFORT_INVALID")
            raw = canonical_json_bytes(payload)
            headers = httpx.Headers(request.headers)
            headers.pop("content-length", None)
            outgoing = httpx.Request(request.method, request.url, headers=headers,
                                     content=raw, extensions=request.extensions)
            self.count += 1
            if trace_root is not None:
                write_once(trace_root / f"wire-{self.count:03d}.json", canonical_json_bytes({
                    "schema_version": "finresearchops.model-wire/v2", "model": payload.get("model"),
                    "max_tokens": payload["max_tokens"], "removed_max_completion_tokens": removed_alias,
                    "reasoning_effort": payload.get("reasoning_effort", "provider_default"), "payload_bytes": len(raw)}))
            return self.inner.handle_request(outgoing)

        def close(self):
            self.inner.close()

    return httpx.Client(timeout=120, transport=DeepSeekLimitTransport())
