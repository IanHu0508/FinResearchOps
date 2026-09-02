import unittest

from finauditgate.adapters.ollama_contract import (
    CANDIDATE_TOOL_CONTRACT,
    ToolContractError,
)


DOCUMENT = (
    b"metric=Revenue;basis=Reported;period=FY2024;value=100;currency=USD;"
    b"unit=Monetary;scale=Million;sign=Positive\n"
    b"metric=Revenue;basis=Reported;period=FY2025;value=120;currency=USD;"
    b"unit=Monetary;scale=Million;sign=Positive\n"
)


def _arguments() -> dict[str, object]:
    return {
        "evidence": [
            {
                "evidence_id": "prior",
                "exact_span": DOCUMENT.splitlines()[0].decode("utf-8"),
                "metric": "Revenue",
                "metric_basis": "Reported",
                "period": "FY2024",
                "value": "100",
                "currency": "USD",
                "unit": "Monetary",
                "scale": "Million",
                "sign": "Positive",
            },
            {
                "evidence_id": "current",
                "exact_span": DOCUMENT.splitlines()[1].decode("utf-8"),
                "metric": "Revenue",
                "metric_basis": "Reported",
                "period": "FY2025",
                "value": "120",
                "currency": "USD",
                "unit": "Monetary",
                "scale": "Million",
                "sign": "Positive",
            },
        ],
        "calculation": {
            "operation": "growth_rate_percent",
            "operand_ids": ["current", "prior"],
            "output_unit": "PERCENT",
            "quantize": "0.01",
        },
    }


class OllamaToolContractTest(unittest.TestCase):
    def test_one_contract_generates_schema_and_decodes_candidate(self) -> None:
        tool_schema = CANDIDATE_TOOL_CONTRACT.tool_schema()
        candidate = CANDIDATE_TOOL_CONTRACT.decode(_arguments(), DOCUMENT)

        self.assertFalse(
            tool_schema["function"]["parameters"]["additionalProperties"]
        )
        self.assertEqual(2, len(candidate.evidence))
        self.assertEqual(("current", "prior"), candidate.calculation.operand_ids)

    def test_unknown_and_missing_fields_fail_closed(self) -> None:
        unknown = _arguments()
        unknown["unexpected"] = True
        with self.assertRaises(ToolContractError) as extra:
            CANDIDATE_TOOL_CONTRACT.decode(unknown, DOCUMENT)
        self.assertEqual("TOOL_ARGUMENT_SHAPE_INVALID", extra.exception.code)

        missing = _arguments()
        del missing["calculation"]
        with self.assertRaises(ToolContractError) as absent:
            CANDIDATE_TOOL_CONTRACT.decode(missing, DOCUMENT)
        self.assertEqual("TOOL_ARGUMENT_SHAPE_INVALID", absent.exception.code)

    def test_wrong_type_and_enum_fail_closed(self) -> None:
        wrong_type = _arguments()
        wrong_type["evidence"][0]["value"] = 100
        with self.assertRaises(ToolContractError) as typed:
            CANDIDATE_TOOL_CONTRACT.decode(wrong_type, DOCUMENT)
        self.assertEqual("EVIDENCE_ARGUMENT_SHAPE_INVALID", typed.exception.code)

        wrong_enum = _arguments()
        wrong_enum["calculation"]["operation"] = "percentage_change"
        with self.assertRaises(ToolContractError) as enumerated:
            CANDIDATE_TOOL_CONTRACT.decode(wrong_enum, DOCUMENT)
        self.assertEqual("TOOL_ARGUMENT_NOT_ALLOWLISTED", enumerated.exception.code)


if __name__ == "__main__":
    unittest.main()
