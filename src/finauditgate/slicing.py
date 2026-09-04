"""Choose the document region a task should see, deterministically.

A financial figure only means something next to the lines that say which period
it belongs to, what currency it is in and at what scale.  Two runs on the
development filing proved it: the same two numbers were read correctly with the
header lines in the slice and misread without them.  Until now a person picked
that region by hand, which is the largest piece of unautomated work in the
pipeline and the easiest place for a slice to be widened after a failure.

This module is preparation, not adjudication.  It decides what the model is
shown; the reviewed validation profile still decides what is true, and nothing
here can make the gate accept anything it would otherwise refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# A statement header names the span of time or the balance date.  These are the
# forms that appear in an annual report or a Form 20-F; the list is deliberately
# short and literal rather than a general date parser.
_PERIOD_HEADER = re.compile(
    r"\b(?:for\s+the\s+)?"
    r"(?:years?|quarter|three\s+months|six\s+months|nine\s+months)\s+ended\b"
    r"|\bas\s+(?:of|at)\b"
    # A balance sheet may head its columns with the date alone -- "December 31,"
    # over the year row -- where another filing writes "As of December 31,".
    # A month and day with no year is a column heading, not prose.
    r"|\b(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2}\s*,\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# A line that is only a page number, or a running header, ends the table above.
_HARD_BOUNDARY = re.compile(
    r"^\s*(?:table\s+of\s+contents|f?-?\d{1,4})\s*$", re.IGNORECASE
)
# Two or more four-digit years on one line is a column header.
_YEAR_ROW = re.compile(r"\b(?:19|20)\d{2}\b")
# A statement says its units once, above its period header -- "(in thousands)",
# "(All amounts in thousands…)", "RMB'Million". A slice that starts at the
# period header cuts it off, and then nothing in the slice says what scale the
# figures are in. Both filings measured so far put it within a few lines above.
_UNITS_STATEMENT = re.compile(
    r"\b(?:in\s+)?(?:thousands?|millions?|billions?)\b|['’]0{3}\b|['’]Million\b",
    re.IGNORECASE,
)
_UNITS_LOOKBACK = 6

MAX_LOOKBACK_LINES = 60
_SECTION_LOOKBACK = 400
_MAX_TABLE_TAIL = 40
_DRY_LINES = 2
TRAILING_LINES = 2


@dataclass(frozen=True, slots=True)
class DocumentSlice:
    """One byte range of a document, and why it starts where it does."""

    byte_start: int
    byte_end: int
    first_line: int
    last_line: int
    header_line: int | None
    reason: str

    def text(self, document: bytes) -> bytes:
        return document[self.byte_start : self.byte_end]


class SliceNotUnique(ValueError):
    """The anchor does not identify exactly one line.

    This is the normal case for a filing, not an edge case: the same row is
    printed in the income statement, the comprehensive-income statement, the
    cash-flow statement and again in the narrative, so a row alone cannot say
    where it is.  The caller must add a section.
    """

    def __init__(self, message: str, candidates: tuple["DocumentSlice", ...]) -> None:
        self.candidates = candidates
        super().__init__(message)


def locate_slices(
    document: bytes,
    row: str,
    *,
    max_bytes: int,
    section: str | None = None,
    trailing_lines: int = TRAILING_LINES,
) -> tuple[DocumentSlice, ...]:
    """Every region whose row matches, one per place the row is printed.

    A region runs from the nearest period header above the row -- stopping at a
    page boundary so it cannot reach into the table before it -- down to a few
    lines past the row.  `section` keeps only the regions whose nearest heading
    above contains it, which is how one of several identical rows is named.
    """

    lines = document.decode("utf-8").split("\n")
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line.encode("utf-8")) + 1

    found = []
    for anchor_index, line in enumerate(lines):
        if row not in line:
            continue
        header_index, reason = None, "no header found above the row"
        floor = max(0, anchor_index - MAX_LOOKBACK_LINES)
        for index in range(anchor_index, floor - 1, -1):
            if index != anchor_index and _HARD_BOUNDARY.match(lines[index]):
                reason = "stopped at a page boundary before any header"
                break
            if _PERIOD_HEADER.search(lines[index]):
                header_index, reason = index, "started at the period header"
                break
        if section is not None and not _within_section(lines, anchor_index, section):
            continue
        first = header_index if header_index is not None else max(0, anchor_index - 1)
        first = _include_units_statement(lines, first)
        last = _end_of_table(lines, anchor_index, trailing_lines)
        byte_start = starts[first]
        byte_end = starts[last] + len(lines[last].encode("utf-8"))
        if byte_end - byte_start > max_bytes:
            while last > anchor_index and byte_end - byte_start > max_bytes:
                last -= 1
                byte_end = starts[last] + len(lines[last].encode("utf-8"))
            while first < anchor_index and byte_end - byte_start > max_bytes:
                first += 1
                byte_start = starts[first]
            reason += "; truncated to the byte budget"
        found.append(
            DocumentSlice(
                byte_start=byte_start,
                byte_end=byte_end,
                first_line=first + 1,
                last_line=last + 1,
                header_line=None if header_index is None else header_index + 1,
                reason=reason,
            )
        )
    return tuple(found)


def locate_slice(
    document: bytes,
    row: str,
    *,
    max_bytes: int,
    section: str | None = None,
    trailing_lines: int = TRAILING_LINES,
) -> DocumentSlice:
    """The one region the row and section name, or a refusal naming the rest."""

    candidates = locate_slices(
        document,
        row,
        max_bytes=max_bytes,
        section=section,
        trailing_lines=trailing_lines,
    )
    if len(candidates) != 1:
        raise SliceNotUnique(
            f"row matches {len(candidates)} regions"
            + ("" if section is None else f" within section {section!r}"),
            candidates,
        )
    return candidates[0]


def _include_units_statement(lines: list[str], first: int) -> int:
    """Extend the start upward to the statement's units line, if it is there.

    The scale a figure is denominated in is stated once, above the period
    header, and a slice that begins at the period header does not contain it.
    A model shown such a slice cannot name the scale from the document, and its
    answer is then a guess the gate rightly refuses -- which looks like a
    labelling failure and is really a slicing one.
    """

    for candidate in range(first - 1, max(-1, first - 1 - _UNITS_LOOKBACK), -1):
        line = lines[candidate]
        if _HARD_BOUNDARY.match(line):
            break
        if _UNITS_STATEMENT.search(line):
            return candidate
    return first


def _end_of_table(lines: list[str], index: int, minimum: int) -> int:
    """Follow the table past the row, to where its figures stop.

    A reviewer includes the rest of the block a row sits in -- the remaining
    lines of the statement and the footnote that qualifies them -- because that
    is what makes the row legible.  The table is taken to end after a run of
    lines carrying no figures, or at a page boundary.
    """

    last = min(len(lines) - 1, index + minimum)
    dry = 0
    for candidate in range(index + 1, min(len(lines), index + 1 + _MAX_TABLE_TAIL)):
        line = lines[candidate]
        if _HARD_BOUNDARY.match(line):
            break
        if not line.strip():
            # A layout-preserving extractor puts blank lines between rows; they
            # are spacing, not the end of the table.
            last = candidate
            continue
        dry = 0 if any(c.isdigit() for c in line) else dry + 1
        if dry > _DRY_LINES:
            break
        last = candidate
    return max(last, min(len(lines) - 1, index + minimum))


def _within_section(lines: list[str], index: int, section: str) -> bool:
    """True when `section` is named above the row, within the same page block.

    The window stops at the page boundary above the row rather than running a
    fixed number of lines, because a statement title printed above one table
    would otherwise also appear to cover every table after it.  No rule
    separated real headings from ordinary row labels reliably enough to trust,
    so this is a containment test over a bounded window, and anything still
    ambiguous is refused rather than guessed.
    """

    folded = section.casefold()
    for candidate in range(index, max(0, index - _SECTION_LOOKBACK) - 1, -1):
        line = lines[candidate]
        if candidate != index and _HARD_BOUNDARY.match(line):
            return False
        if folded in line.casefold():
            return True
    return False


def describes_a_year_column(line: str) -> bool:
    """True when a line looks like the year-column row of a statement."""

    return len(_YEAR_ROW.findall(line)) >= 2
