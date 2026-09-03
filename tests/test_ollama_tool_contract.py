import unittest

from finauditgate.adapters.ollama_contract import (
    CANDIDATE_TOOL_CONTRACT,
    EVIDENCE_IDS,
    METRIC_BASES,
    METRICS,
    SCALES,
    SIGNS,
    UNITS,
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
                "evidence_id": "comparison",
                "exact_span": DOCUMENT.splitlines()[0].decode("utf-8"),
                "metric": "revenue",
                "metric_basis": "REPORTED",
                "period": "FY2024",
                "value": "100",
                "currency": "USD",
                "unit": "MONETARY",
                "scale": "MILLION",
                "sign": "POSITIVE",
            },
            {
                "evidence_id": "current",
                "exact_span": DOCUMENT.splitlines()[1].decode("utf-8"),
                "metric": "revenue",
                "metric_basis": "REPORTED",
                "period": "FY2025",
                "value": "120",
                "currency": "USD",
                "unit": "MONETARY",
                "scale": "MILLION",
                "sign": "POSITIVE",
            },
        ],
        "calculation": {
            "operation": "growth_rate_percent",
            "operand_ids": ["current", "comparison"],
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
        self.assertEqual(
            ("current", "comparison"),
            candidate.calculation.operand_ids,
        )

    def test_schema_publishes_the_closed_vocabulary(self) -> None:
        parameters = CANDIDATE_TOOL_CONTRACT.tool_schema()["function"]["parameters"]
        evidence = parameters["properties"]["evidence"]["items"]["properties"]
        operand_items = parameters["properties"]["calculation"]["properties"][
            "operand_ids"
        ]["items"]

        self.assertEqual(list(EVIDENCE_IDS), evidence["evidence_id"]["enum"])
        self.assertEqual(list(EVIDENCE_IDS), operand_items["enum"])
        self.assertEqual(list(METRICS), evidence["metric"]["enum"])
        self.assertEqual(list(METRIC_BASES), evidence["metric_basis"]["enum"])
        self.assertEqual(list(UNITS), evidence["unit"]["enum"])
        self.assertEqual(list(SCALES), evidence["scale"]["enum"])
        self.assertEqual(list(SIGNS), evidence["sign"]["enum"])
        for free_text in ("exact_span", "period", "value", "currency"):
            self.assertNotIn("enum", evidence[free_text])
            self.assertTrue(evidence[free_text]["description"])

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

    def test_vocabulary_outside_the_enumerations_fails_closed(self) -> None:
        for field_name, label in (
            ("evidence_id", "revenue_2025"),
            ("metric", "Revenues"),
            ("metric_basis", "Year ended 31 December"),
            ("unit", "RMB’Million"),
            ("scale", "Million"),
            ("sign", "+"),
        ):
            with self.subTest(field=field_name):
                arguments = _arguments()
                arguments["evidence"][1][field_name] = label
                with self.assertRaises(ToolContractError) as rejected:
                    CANDIDATE_TOOL_CONTRACT.decode(arguments, DOCUMENT)
                self.assertEqual(
                    "TOOL_ARGUMENT_NOT_ALLOWLISTED",
                    rejected.exception.code,
                )

        free_ids = _arguments()
        free_ids["calculation"]["operand_ids"] = ["revenue_2025", "comparison"]
        with self.assertRaises(ToolContractError) as operand:
            CANDIDATE_TOOL_CONTRACT.decode(free_ids, DOCUMENT)
        self.assertEqual("TOOL_ARGUMENT_NOT_ALLOWLISTED", operand.exception.code)


if __name__ == "__main__":
    unittest.main()
