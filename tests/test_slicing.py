import unittest

from finauditgate.slicing import (
    DocumentSlice,
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


if __name__ == "__main__":
    unittest.main()
