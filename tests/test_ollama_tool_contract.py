import random
import unittest

from finauditgate.adapters.ollama_contract import (
    CURRENCIES,
    _locate_span,
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
        response_schema = CANDIDATE_TOOL_CONTRACT.response_schema()
        candidate = CANDIDATE_TOOL_CONTRACT.decode(_arguments(), DOCUMENT)

        self.assertFalse(response_schema["additionalProperties"])
        self.assertEqual(2, len(candidate.evidence))
        self.assertEqual(
            ("current", "comparison"),
            candidate.calculation.operand_ids,
        )

    def test_schema_publishes_the_closed_vocabulary(self) -> None:
        parameters = CANDIDATE_TOOL_CONTRACT.response_schema()
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
        # currency closed on 2026-09-05: it was the last free-text semantic
        # field, and a description on a free-text field has never moved a model
        # here, while one on an enumerated field has.
        self.assertEqual(list(CURRENCIES), evidence["currency"]["enum"])
        for free_text in ("exact_span", "period", "value"):
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

    def test_per_share_and_per_depositary_share_are_distinguishable(self) -> None:
        """A filing may print the two blocks with identical row labels.

        Without a separate term the vocabulary cannot say which block a figure
        came from, so no reviewer can write an answer key the model could hit.
        The unit carries the distinction; the metric stays the same.
        """

        self.assertIn("PER_SHARE", UNITS)
        self.assertIn("PER_DEPOSITARY_SHARE", UNITS)

        for unit in ("PER_SHARE", "PER_DEPOSITARY_SHARE"):
            with self.subTest(unit=unit):
                arguments = _arguments()
                for evidence in arguments["evidence"]:
                    evidence["metric"] = "basic_eps"
                    evidence["unit"] = unit
                    evidence["scale"] = "UNIT"
                candidate = CANDIDATE_TOOL_CONTRACT.decode(arguments, DOCUMENT)
                self.assertEqual(unit, candidate.evidence[0].unit)

        description = CANDIDATE_TOOL_CONTRACT.response_schema()["properties"][
            "evidence"
        ]["items"]["properties"]["unit"]["description"]
        self.assertIn("depositary share", description)

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


def _exact_placements(span: bytes, document: bytes) -> list[int]:
    """Every offset where the exact bytes sit, counting overlaps."""

    offsets, start = [], document.find(span)
    while start >= 0:
        offsets.append(start)
        start = document.find(span, start + 1)
    return offsets


class EvidenceSpanLocationTest(unittest.TestCase):
    """A cited span may be spaced differently; it may never be ambiguous."""

    # The shape a text extractor produces for a wrapped table row: the label
    # sits on its own line and the two figures share the next one.
    WRAPPED = (
        b"Revenue          FY2025          FY2024\n"
        b"Revenues\n"
        b"751,766          660,257\n"
    )

    def _arguments(self, comparison: str, current: str) -> dict[str, object]:
        arguments = _arguments()
        arguments["evidence"][0]["exact_span"] = comparison
        arguments["evidence"][0]["value"] = "660257"
        arguments["evidence"][1]["exact_span"] = current
        arguments["evidence"][1]["value"] = "751766"
        return arguments

    def test_collapsed_whitespace_locates_the_documents_own_bytes(self) -> None:
        candidate = CANDIDATE_TOOL_CONTRACT.decode(
            self._arguments("751,766 660,257", "Revenues 751,766"),
            self.WRAPPED,
        )

        comparison, current = candidate.evidence
        self.assertEqual(
            b"751,766          660,257",
            self.WRAPPED[comparison.byte_start:comparison.byte_end],
        )
        self.assertEqual(
            b"Revenues\n751,766",
            self.WRAPPED[current.byte_start:current.byte_end],
        )

    def test_a_located_span_round_trips_through_the_document(self) -> None:
        arguments = self._arguments("751,766 660,257", "Revenues 751,766")
        candidate = CANDIDATE_TOOL_CONTRACT.decode(arguments, self.WRAPPED)

        encoded = CANDIDATE_TOOL_CONTRACT.encode(candidate, self.WRAPPED)

        self.assertEqual(
            "751,766          660,257",
            encoded["evidence"][0]["exact_span"],
        )
        self.assertEqual(
            candidate,
            CANDIDATE_TOOL_CONTRACT.decode(encoded, self.WRAPPED),
        )

    def test_a_span_that_collapses_onto_two_regions_is_refused(self) -> None:
        document = b"FY2024      660,257\nFY2024   660,257\nFY2025 751,766\n"

        with self.assertRaises(ToolContractError) as ambiguous:
            CANDIDATE_TOOL_CONTRACT.decode(
                self._arguments("FY2024 660,257", "FY2025 751,766"),
                document,
            )

        self.assertEqual("EVIDENCE_SPAN_NOT_UNIQUE", ambiguous.exception.code)

    def test_a_repeated_exact_span_is_still_refused(self) -> None:
        document = b"FY2024 660,257\nFY2024 660,257\nFY2025 751,766\n"

        with self.assertRaises(ToolContractError) as repeated:
            CANDIDATE_TOOL_CONTRACT.decode(
                self._arguments("FY2024 660,257", "FY2025 751,766"),
                document,
            )

        self.assertEqual("EVIDENCE_SPAN_NOT_UNIQUE", repeated.exception.code)

    def test_tolerance_never_admits_a_span_the_exact_search_refused(self) -> None:
        """Tolerance may add spans, never disambiguate one.

        Every span the byte-exact search accepted must resolve to the same
        offsets, and the only spans the tolerant pass may add are those whose
        exact bytes occur nowhere: an ambiguous span stays a refusal.
        Placements are counted overlap-aware, the way `_locate_span` counts
        them, so a span that repeats by overlapping itself is ambiguous here
        too.
        """

        words = ("Revenue", "Revenues", "751,766", "660,257", "FY2025", "shares)")
        spacing = (" ", "  ", "\n", "\n   ", "\t", "     ")
        generator = random.Random(20260903)

        def phrase(count: int) -> bytes:
            return "".join(
                generator.choice(words) + generator.choice(spacing)
                for _ in range(count)
            ).encode("utf-8")

        for _ in range(4_000):
            document = phrase(generator.randint(2, 9))
            span = phrase(generator.randint(1, 3)).strip()
            if not span:
                continue
            exact = _exact_placements(span, document)
            try:
                located = _locate_span(span, document)
            except ToolContractError as refused:
                self.assertEqual("EVIDENCE_SPAN_NOT_UNIQUE", refused.code)
                continue
            if len(exact) == 1:
                self.assertEqual((exact[0], exact[0] + len(span)), located)
            else:
                # Tolerance may only reach spans the exact bytes never match;
                # an ambiguous citation must have been refused above.
                self.assertEqual([], exact)
                self.assertLess(located[0], located[1])

    def test_a_span_that_repeats_by_overlapping_itself_is_refused(self) -> None:
        # "751,766 751,766" sits at two overlapping placements in this row; a
        # non-overlapping scan sees only the first and would resolve it.
        document = b"shares) 751,766 751,766 751,766\n660,257 660,257 x\n"
        self.assertEqual([8, 16], _exact_placements(b"751,766 751,766", document))

        with self.assertRaises(ToolContractError) as overlapping:
            _locate_span(b"751,766 751,766", document)

        self.assertEqual(
            "EVIDENCE_SPAN_NOT_UNIQUE",
            overlapping.exception.code,
        )

    def test_a_collapsed_span_with_two_placements_is_refused(self) -> None:
        # The exact bytes appear nowhere, so the tolerant pass runs; its two
        # placements overlap, and a non-overlapping scan would see only one.
        document = b"shares)  751,766  751,766  751,766\n"
        self.assertEqual([], _exact_placements(b"751,766 751,766", document))

        with self.assertRaises(ToolContractError) as ambiguous:
            _locate_span(b"751,766 751,766", document)

        self.assertEqual("EVIDENCE_SPAN_NOT_UNIQUE", ambiguous.exception.code)

    def test_an_absent_span_is_refused(self) -> None:
        with self.assertRaises(ToolContractError) as absent:
            CANDIDATE_TOOL_CONTRACT.decode(
                self._arguments("FY2023 600,000", "Revenues 751,766"),
                self.WRAPPED,
            )

        self.assertEqual("EVIDENCE_SPAN_NOT_UNIQUE", absent.exception.code)


if __name__ == "__main__":
    unittest.main()
