"""Conservative per-request reservations and explicit token/call limits."""

from copy import deepcopy
from decimal import Decimal


class ModelBudget:
    def __init__(self, *, ceiling_cny="50", input_per_million="9", output_per_million="27",
                 max_calls=24, max_input_bytes=90000, max_output_tokens=8192):
        try:
            self.ceiling = None if ceiling_cny is None else Decimal(ceiling_cny)
            self.input_rate = Decimal(input_per_million)
            self.output_rate = Decimal(output_per_million)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("MODEL_BUDGET_INVALID") from exc
        if (not all(x.is_finite() and x >= 0 for x in (self.input_rate, self.output_rate))
                or (self.ceiling is not None and (not self.ceiling.is_finite() or self.ceiling <= 0))
                or type(max_calls) is not int or not 1 <= max_calls <= 24
                or type(max_input_bytes) is not int or not 1 <= max_input_bytes <= 524288
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536):
            raise ValueError("MODEL_BUDGET_INVALID")
        self.max_calls, self.max_input_bytes, self.max_output_tokens = max_calls, max_input_bytes, max_output_tokens
        self.reserved = Decimal(0)
        self.calls = 0
        self.usage = []
        self._blocked = None
        self._truncated_retry_used = False
        self._last_truncated = False

    def reserve(self, prompt):
        if self._blocked:
            raise ValueError(self._blocked)
        size = len(prompt.encode("utf-8"))
        if size > self.max_input_bytes:
            raise ValueError("MODEL_INPUT_LIMIT")
        if self.calls >= self.max_calls:
            raise ValueError("MODEL_CALL_LIMIT")
        # UTF-8 bytes deliberately overestimate ordinary text tokens. Additional
        # allowance covers message framing and structured-output schema tokens.
        upper = ((Decimal(size + 8192) * self.input_rate
                  + Decimal(self.max_output_tokens) * self.output_rate) / Decimal(1000000))
        if self.ceiling is not None and self.reserved + upper > self.ceiling:
            raise ValueError("MODEL_SPEND_LIMIT")
        self.reserved += upper
        self.calls += 1
        return self.calls

    def record_usage(self, usage, *, truncated=False):
        self._last_truncated = truncated
        clean = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                clean[key] = value
        self.usage.append(clean)
        if clean.get("output_tokens", 0) > self.max_output_tokens:
            self._blocked = "MODEL_OUTPUT_LIMIT_VIOLATION"
        elif truncated:
            self._blocked = "MODEL_OUTPUT_TRUNCATED"
        observed = sum((Decimal(x.get("input_tokens", 0)) * self.input_rate
                        + Decimal(x.get("output_tokens", 0)) * self.output_rate
                        for x in self.usage), Decimal(0)) / Decimal(1000000)
        self.reserved = max(self.reserved, observed)
        return self._blocked is None

    def allow_truncated_retry(self, *, confirmed_length=False) -> bool:
        """Permit one explicit recovery without resetting usage or any limit."""
        # A provider can report a length-finished response slightly above the
        # requested cap. The final-report caller may explicitly recover that
        # confirmed failure once; actual usage, spend and all caps remain intact.
        if self._truncated_retry_used or not self.release_confirmed_truncation(confirmed_length=confirmed_length):
            return False
        self._truncated_retry_used = True
        return True

    def release_confirmed_truncation(self, *, confirmed_length):
        """Called only after the thesis recovery policy reserves an allowance."""
        recoverable = self._blocked == "MODEL_OUTPUT_TRUNCATED" or (
            confirmed_length and self._last_truncated and self._blocked == "MODEL_OUTPUT_LIMIT_VIOLATION")
        if not recoverable:
            return False
        self._blocked = None
        return True

    def checkpoint(self):
        return {"version": "model-budget-checkpoint/v1", "receipt": deepcopy(self.receipt()),
                "limits": {"max_calls": self.max_calls, "max_input_bytes": self.max_input_bytes,
                           "max_output_tokens": self.max_output_tokens},
                "blocked": self._blocked, "last_truncated": self._last_truncated,
                "legacy_truncated_retry_used": self._truncated_retry_used}

    def restore(self, checkpoint):
        """Continue the same current logical budget, including unknown-call reserves."""
        if self.calls or self.usage or self.reserved:
            raise ValueError("MODEL_RESUME_BUDGET_NOT_EMPTY")
        if (not isinstance(checkpoint, dict) or set(checkpoint) != {"version", "receipt", "limits", "blocked", "last_truncated", "legacy_truncated_retry_used"}
                or checkpoint["version"] != "model-budget-checkpoint/v1"
                or checkpoint["limits"] != self.checkpoint()["limits"]
                or checkpoint["blocked"] not in (None, "MODEL_OUTPUT_TRUNCATED", "MODEL_OUTPUT_LIMIT_VIOLATION")
                or type(checkpoint["last_truncated"]) is not bool or type(checkpoint["legacy_truncated_retry_used"]) is not bool):
            raise ValueError("MODEL_RESUME_BUDGET_MISMATCH")
        prior = checkpoint["receipt"]
        current = self.receipt()
        if (not isinstance(prior, dict) or any(prior.get(k) != current[k] for k in
                ("ceiling_cny", "input_per_million_cny", "output_per_million_cny", "pricing_basis"))
                or type(prior.get("calls")) is not int or not 0 <= prior["calls"] <= self.max_calls
                or not isinstance(prior.get("usage"), list) or len(prior["usage"]) > prior["calls"]):
            raise ValueError("MODEL_RESUME_BUDGET_MISMATCH")
        usage = []
        for row in prior["usage"]:
            if (not isinstance(row, dict) or not row.keys() <= {"input_tokens", "output_tokens", "total_tokens"}
                    or any(type(v) is not int or v < 0 for v in row.values())):
                raise ValueError("MODEL_RESUME_BUDGET_MISMATCH")
            usage.append(dict(row))
        try:
            reserved = Decimal(prior["reserved_upper_cny"])
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise ValueError("MODEL_RESUME_BUDGET_MISMATCH") from exc
        observed = sum((Decimal(r.get("input_tokens", 0)) * self.input_rate
                        + Decimal(r.get("output_tokens", 0)) * self.output_rate for r in usage), Decimal(0)) / Decimal(1000000)
        minimum_reserved = Decimal(prior["calls"]) * (Decimal(8192) * self.input_rate
            + Decimal(self.max_output_tokens) * self.output_rate) / Decimal(1000000)
        if not reserved.is_finite() or reserved < max(observed, minimum_reserved):
            raise ValueError("MODEL_RESUME_BUDGET_MISMATCH")
        self.calls, self.usage, self.reserved = prior["calls"], usage, reserved
        self._blocked = checkpoint["blocked"]
        self._last_truncated = checkpoint["last_truncated"]
        self._truncated_retry_used = checkpoint["legacy_truncated_retry_used"]

    def receipt(self):
        complete = bool(self.usage) and len(self.usage) == self.calls and all(
            "input_tokens" in x and "output_tokens" in x for x in self.usage)
        estimate = None
        if complete:
            estimate = str(sum((Decimal(x["input_tokens"]) * self.input_rate
                                + Decimal(x["output_tokens"]) * self.output_rate
                                for x in self.usage), Decimal(0)) / Decimal(1000000))
        return {"calls": self.calls, "ceiling_cny": None if self.ceiling is None else str(self.ceiling),
                "reserved_upper_cny": str(self.reserved), "usage": self.usage,
                "uncached_price_estimate_cny": estimate,
                "input_per_million_cny": str(self.input_rate), "output_per_million_cny": str(self.output_rate),
                "pricing_basis": "CONFIGURED_RATES_NOT_PROVIDER_INVOICE"}
