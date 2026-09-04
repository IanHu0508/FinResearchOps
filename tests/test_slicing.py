import unittest

from finauditgate.slicing import (
    _include_units_statement,
    DocumentSlice,
    missing_semantics,
    SliceNotUnique,
    locate_slice,
    locate_slices,
)


# A statement in the shape both real filings use: a title, a period header, a
# year row, a currency and scale row, then the figures.
STATEMENT = (
    b"Table of Contents\n"
    b"CONSOLIDATED INCOME STATEMENTS\n"
    b"Year ended 31 December\n"
    b"2025    2024\n"
    b"RMB'Million    RMB'Million\n"
    b"Revenues    751,766    660,257\n"
    b"Gross profit    400,000    350,000\n"
    b"\n"
    b"Each share represents one vote.\n"
    b"127\n"
    b"Table of Contents\n"
    b"CONSOLIDATED STATEMENTS OF CASH FLOWS\n"
    b"Year ended 31 December\n"
    b"2025    2024\n"
    b"RMB'Million    RMB'Million\n"
    b"Revenues    751,766    660,257\n"
)


class SlicingTest(unittest.TestCase):
    def test_a_row_is_cut_together_with_its_period_header(self) -> None:
        """A figure without its header is the failure this exists to remove."""

        found = locate_slices(
            STATEMENT, "Gross profit    400,000", max_bytes=32768
        )

        self.assertEqual(1, len(found))
        text = found[0].text(STATEMENT).decode("utf-8")
        self.assertIn("Year ended 31 December", text)
        self.assertIn("RMB'Million", text)
        self.assertIn("2025    2024", text)
        self.assertIn("400,000", text)
        self.assertEqual("started at the period header", found[0].reason)

    def test_the_slice_reaches_the_statement_units_line(self) -> None:
        """A statement says its scale once, above the period header.

        A slice that starts at the period header does not contain it, and a
        model shown such a slice cannot name the scale from the document. Its
        answer is then a guess, the gate refuses it, and the refusal looks like
        a labelling failure when it is really a slicing one. Both filings
        measured so far place the units line a few lines above the header.
        """

        statement = (
            b"CONSOLIDATED STATEMENTS OF INCOME\n"
            b"(All amounts in thousands, except per share data)\n"
            b"Year Ended December 31,\n"
            b"2024    2025\n"
            b"RMB    RMB\n"
            b"Total net revenues    108,420,832    105,919,546\n"
        )
        found = locate_slice(
            statement, "Total net revenues    108,420,832", max_bytes=6144
        )

        text = found.text(statement).decode("utf-8")
        self.assertIn("in thousands", text)
        self.assertIn("Year Ended December 31,", text)
        self.assertEqual(2, found.first_line)

    def test_the_units_search_does_not_cross_a_page_boundary(self) -> None:
        """The previous statement's units line is not this statement's."""

        spanning = (
            b"(All amounts in millions)\n"
            b"127\n"
            b"Year Ended December 31,\n"
            b"2024    2025\n"
            b"Total net revenues    108,420,832    105,919,546\n"
        )
        found = locate_slice(
            spanning, "Total net revenues    108,420,832", max_bytes=6144
        )

        self.assertNotIn("millions", found.text(spanning).decode("utf-8"))

    def test_a_repeated_row_yields_one_region_per_place_it_is_printed(
        self,
    ) -> None:
        """The same row appears in several statements; guessing is not allowed."""

        found = locate_slices(
            STATEMENT, "Revenues    751,766    660,257", max_bytes=32768
        )

        self.assertEqual(2, len(found))
        with self.assertRaises(SliceNotUnique) as ambiguous:
            locate_slice(
                STATEMENT, "Revenues    751,766    660,257", max_bytes=32768
            )
        self.assertEqual(2, len(ambiguous.exception.candidates))

    def test_a_section_names_which_of_the_repeated_rows_is_meant(self) -> None:
        chosen = locate_slice(
            STATEMENT,
            "Revenues    751,766    660,257",
            max_bytes=32768,
            section="CASH FLOWS",
        )

        # The section selects which region is meant; the slice itself still
        # starts at that region's period header, not at the heading.
        self.assertEqual(13, chosen.first_line)
        self.assertEqual(13, chosen.header_line)
        income_statement = locate_slice(
            STATEMENT,
            "Revenues    751,766    660,257",
            max_bytes=32768,
            section="INCOME STATEMENTS",
        )
        self.assertEqual(3, income_statement.first_line)

    def test_a_bare_date_heads_a_table_just_as_a_worded_one_does(self) -> None:
        """One filing writes "As of December 31,", another just the date.

        Both are the heading of a balance sheet's columns, and a slice that
        loses it loses the period the figures belong to.
        """

        bare = (
            b"CONSOLIDATED BALANCE SHEETS\n"
            b"December 31,\n"
            b"2024    2025\n"
            b"RMB    RMB\n"
            b"Total assets    195,991,550    221,415,060\n"
        )
        found = locate_slice(
            bare, "Total assets    195,991,550", max_bytes=6144
        )

        text = found.text(bare).decode("utf-8")
        self.assertEqual(2, found.header_line)
        self.assertIn("December 31,", text)
        self.assertIn("RMB", text)

    def test_a_date_inside_a_sentence_does_not_head_a_table(self) -> None:
        """Prose mentioning a date is not a column heading."""

        prose = (
            b"As described above, on December 31, 2025 the group completed a\n"
            b"reorganisation of its subsidiaries.\n"
            b"Total assets    195,991,550    221,415,060\n"
        )
        found = locate_slice(
            prose, "Total assets    195,991,550", max_bytes=6144
        )

        self.assertIsNone(found.header_line)

    def test_a_header_is_never_taken_from_the_table_before_it(self) -> None:
        """A page boundary stops the search, so a slice cannot span statements."""

        orphan = b"127\nTable of Contents\nRevenues    10    9\n"
        found = locate_slices(orphan, "Revenues    10    9", max_bytes=32768)

        self.assertEqual(1, len(found))
        self.assertIsNone(found[0].header_line)
        self.assertNotIn("Year ended", found[0].text(orphan).decode("utf-8"))

    def test_blank_lines_inside_a_table_do_not_end_it(self) -> None:
        """A layout-preserving extractor spaces rows out; that is not the end."""

        spaced = (
            b"Year ended 31 December\n"
            b"2025    2024\n"
            b"Revenues    751,766    660,257\n"
            b"\n"
            b"\n"
            b"\n"
            b"Gross profit    400,000    350,000\n"
        )
        found = locate_slice(
            spaced, "Revenues    751,766", max_bytes=32768
        )

        self.assertIn("400,000", found.text(spaced).decode("utf-8"))

    def test_the_cited_row_survives_the_byte_budget(self) -> None:
        """When the region will not fit, the row is the part that is kept."""

        found = locate_slice(
            STATEMENT, "Gross profit    400,000", max_bytes=60
        )

        text = found.text(STATEMENT).decode("utf-8")
        self.assertLessEqual(len(text.encode("utf-8")), 60)
        self.assertIn("Gross profit    400,000", text)

    def test_a_slice_is_a_byte_range_into_the_document_itself(self) -> None:
        """Offsets must be the document's own, so a locator stays checkable."""

        found = locate_slice(
            STATEMENT, "Gross profit    400,000", max_bytes=32768
        )

        self.assertIsInstance(found, DocumentSlice)
        self.assertEqual(
            STATEMENT[found.byte_start : found.byte_end],
            found.text(STATEMENT),
        )


class SliceSufficiencyTest(unittest.TestCase):
    """A slice must state everything the claim about it will be judged on."""

    COMPLETE = (
        b"(All amounts in thousands)\n"
        b"Year Ended December 31,\n"
        b"2024    2025\n"
        b"RMB    RMB\n"
        b"Total net revenues    108,420,832    105,919,546\n"
    )

    def test_a_complete_slice_is_missing_nothing(self) -> None:
        self.assertEqual(
            (),
            missing_semantics(self.COMPLETE, currency="RMB", scale="THOUSAND"),
        )

    def test_a_currency_does_not_stand_in_for_a_scale(self) -> None:
        """The bug this exists to prevent, stated as a test.

        The check that shipped before asked for a period header and currency
        *or* scale, so a slice naming RMB and no scale passed. Fifteen of
        thirty evidence-bearing cases across three sealed packs were
        unanswerable because of it.
        """

        no_scale = self.COMPLETE.replace(b"(All amounts in thousands)\n", b"")

        self.assertEqual(
            ("scale",),
            missing_semantics(no_scale, currency="RMB", scale="THOUSAND"),
        )

    def test_each_requirement_is_reported_separately(self) -> None:
        bare = b"Total net revenues    108,420,832    105,919,546\n"

        self.assertEqual(
            ("period", "currency", "scale"),
            missing_semantics(bare, currency="RMB", scale="THOUSAND"),
        )

    def test_only_what_the_claim_depends_on_is_required(self) -> None:
        """A share count declares no currency; a per-share amount is unscaled."""

        counts = (
            b"Year Ended December 31,\n"
            b"2024    2025\n"
            b"Weighted average shares    106,074,914    100,072,178\n"
        )
        self.assertEqual(
            (), missing_semantics(counts, currency="NONE", scale="UNIT")
        )
        # the same slice would be incomplete for a monetary claim
        self.assertEqual(
            ("currency", "scale"),
            missing_semantics(counts, currency="RMB", scale="THOUSAND"),
        )

    def test_a_currency_symbol_counts_as_a_currency(self) -> None:
        symbols = (
            b"Year Ended December 31,\n"
            b"US$    US$\n"
            b"(in millions)\n"
            b"Revenue    1    2\n"
        )

        self.assertEqual(
            (), missing_semantics(symbols, currency="USD", scale="MILLION")
        )


if __name__ == "__main__":
    unittest.main()


class UnitsCaptionAboveTheTable(unittest.TestCase):
    """Reaching the caption when the slice starts partway down its table.

    The first version of this looked six lines up, because the two filings
    measured at the time printed the caption within a few lines of the header.
    A third filing prints it thirteen lines up, on the same page, and those
    cases stayed unanswerable after the repair meant to fix exactly them.
    """

    HEADING = [
        "Table of Contents",
        "ACME LIMITED",
        "CONSOLIDATED STATEMENTS OF INCOME",
        "(All amounts in thousands, except per share data)",
        "",
        "Year Ended December 31,",
        "        2023      2024      2025",
        "        RMB       RMB       RMB",
    ]
    ROWS = [
        "Net revenues:",
        "Product revenues     105,613     100,734      97,398",
        "Other revenues         7,242       7,686       8,520",
        "Total net revenues   112,856     108,420     105,919",
        "Cost of revenues     (87,135)    (82,951)    (81,429)",
        "Gross profit          25,720      25,469      24,490",
        "Operating expenses:",
        "Fulfillment expenses   6,900       7,100       7,300",
    ]

    def _document(self, *blocks: list[str]) -> tuple[bytes, list[str]]:
        lines = [line for block in blocks for line in block]
        return "\n".join(lines).encode("utf-8"), lines

    def test_caption_is_reached_past_the_tables_own_heading(self) -> None:
        document, lines = self._document(self.HEADING, self.ROWS)
        start = lines.index("Fulfillment expenses   6,900       7,100       7,300")
        reached = _include_units_statement(lines, start)
        self.assertEqual(lines[reached],
                         "(All amounts in thousands, except per share data)")

    def test_a_second_statements_caption_is_not_taken(self) -> None:
        """Walking out of this table into the one above must stop."""

        upper = [
            "(All amounts in millions)",
            "",
            "Year Ended December 31,",
            "        2023      2024      2025",
            "Revenue              10        11        12",
        ]
        lower = [
            "Year Ended December 31,",
            "        2023      2024      2025",
            "Segment revenue       4         5         6",
        ]
        document, lines = self._document(upper, lower)
        start = lines.index("Segment revenue       4         5         6")
        reached = _include_units_statement(lines, start)
        # The millions caption belongs to the table above, not to this one.
        self.assertNotEqual(lines[reached], "(All amounts in millions)")
        self.assertEqual(reached, start)

    def test_the_page_boundary_stops_the_walk(self) -> None:
        previous_page = ["(All amounts in thousands)", "Table of Contents"]
        document, lines = self._document(previous_page, ["Revenue   10   11"])
        start = lines.index("Revenue   10   11")
        self.assertEqual(_include_units_statement(lines, start), start)

    def test_a_caption_directly_above_is_still_found(self) -> None:
        document, lines = self._document(["(in thousands)", "Revenue   10   11"])
        start = lines.index("Revenue   10   11")
        self.assertEqual(lines[_include_units_statement(lines, start)], "(in thousands)")
