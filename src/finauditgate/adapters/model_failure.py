"""Extract response data carried by SDK errors without serializing a client."""


def completion_failure(error):
    completion = getattr(error, "completion", None)
    if completion is None:
        return None
    # Keep the optional provider dependency out of core and offline readers.
    from openai.types.chat import ChatCompletion

    if not isinstance(completion, ChatCompletion):
        return None
    body = completion.model_dump(mode="json")
    provider_usage = body.get("usage")
    usage = None if provider_usage is None else {
        target: provider_usage[source]
        for source, target in (("prompt_tokens", "input_tokens"),
                               ("completion_tokens", "output_tokens"),
                               ("total_tokens", "total_tokens"))
        if type(provider_usage.get(source)) is int and provider_usage[source] >= 0
    }
    choices = body["choices"]
    finish_reasons = [choice.get("finish_reason") for choice in choices]

    def lengths(field):
        return [len(value) if isinstance(value := choice["message"].get(field), str) else None
                for choice in choices]

    return {"provider_response": body, "provider_usage": provider_usage, "usage": usage,
            "finish_reasons": finish_reasons, "truncated": "length" in finish_reasons,
            "content_characters": lengths("content"),
            "reasoning_characters": lengths("reasoning_content")}
