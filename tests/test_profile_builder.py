"""The profile builder's locator flags, exercised through its command line.

The builder is how every reviewed case enters the system, and until this file
existed nothing tested it.  A corroborating figure could be named two ways --
by a line of text, or by which printing of the value it is -- and only the
second was ever run.  The first raised `ValueError: too many values to unpack`
on every call, because the helper it used returns the line's text rather than
its byte range.  A flag with no test is a flag nobody has run.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
BUILDER = REPO / "scripts" / "build_validation_profile.py"

# A statement whose total is restated, unlabelled, beneath its own breakdown --
# the shape a corroborating figure actually takes in a filing.
DOCUMENT = b"""(in thousands)
                                        2025        2024
Profit for the year                  229,801     196,467
Attributable to:
   Equity holders of the Company     224,842     194,073
   Non-controlling interests           4,959       2,394
                                     229,801     196,467
"""

BASE = [
    "--source-id", "test-issuer",
    "--document-name", "test-issuer__statement.txt",
    "--published-at", "2026-04-09",
    "--cutoff", "2026-05-01",
    "--question", "What was the growth in profit for the year?",
    "--profile-name", "test-issuer/v1",
    "--current-line", "Profit for the year", "--current-value", "229801",
    "--current-period", "FY2025",
    "--comparison-line", "Profit for the year", "--comparison-value", "196467",
    "--comparison-period", "FY2024",
    "--metric", "profit_for_the_year", "--currency", "RMB",
    "--unit", "MONETARY", "--scale", "THOUSAND",
    "--operation", "growth_rate_percent",
]


class ProfileBuilderCorroboration(unittest.TestCase):
    def _build(self, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            document = root / "statement.txt"
            document.write_bytes(DOCUMENT)
            output = root / "profile.json"
            done = subprocess.run(
                [sys.executable, str(BUILDER), "--document", str(document),
                 *BASE, *extra, "--output", str(output)],
                capture_output=True, text=True,
                env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
            )
            done.profile = json.loads(output.read_text()) if output.exists() else None
            return done

    def _corroboration(self, profile: dict) -> dict:
        current = [e for e in profile["evidence_allowlist"] if e["role"] == "CURRENT"]
        self.assertEqual(len(current), 1)
        return current[0]["corroboration"]

    def test_corroboration_named_by_a_line_of_text(self) -> None:
        """--current-corroboration-line resolves to that line's bytes."""

        done = self._build("--current-corroboration-line", "Equity holders of the Company")
        self.assertEqual(done.returncode, 0, done.stderr)
        span = self._corroboration(done.profile)
        cited = DOCUMENT[span["byte_start"]: span["byte_end"]]
        self.assertIn(b"Equity holders of the Company", cited)
        self.assertNotIn(b"\n", cited)

    def test_corroboration_named_by_which_printing_it_is(self) -> None:
        """--current-corroboration-occurrence reaches a line with no unique text."""

        done = self._build("--current-corroboration-occurrence", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        span = self._corroboration(done.profile)
        cited = DOCUMENT[span["byte_start"]: span["byte_end"]]
        self.assertIn(b"229,801", cited)
        # The second printing, not the labelled statement line above it.
        self.assertNotIn(b"Profit for the year", cited)

    def test_a_figure_cannot_corroborate_itself(self) -> None:
        """Naming the evidence's own line adds no independent printing.

        Both flags can reach the statement line the CURRENT evidence already
        cites.  Corroboration means the figure is printed a second time, so a
        span that overlaps the evidence is not corroboration at all, and the
        gate's loader refuses the profile rather than recording agreement a
        reader would take at face value.
        """

        for flag, value in (("--current-corroboration-line", "Profit for the year"),
                            ("--current-corroboration-occurrence", "1")):
            with self.subTest(flag=flag):
                done = self._build(flag, value)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("PRIVATE_VALIDATION_PROFILE_INVALID", done.stderr)

    def test_an_occurrence_that_is_not_printed_is_refused(self) -> None:
        done = self._build("--current-corroboration-occurrence", "3")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("out of range", done.stderr)

    def test_a_corroboration_line_that_is_not_unique_is_refused(self) -> None:
        done = self._build("--current-corroboration-line", "229,801")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("occurs more than once", done.stderr)


if __name__ == "__main__":
    unittest.main()


RATIO_DOCUMENT = b"""(in millions)
                                        2025        2024
Total net revenues                   105,919     108,420
Cost of revenues                     (81,429)    (82,951)
Gross profit                          24,490      25,469
Operating income (loss)               (5,823)     21,270
"""

RATIO_BASE = [
    "--source-id", "test-issuer",
    "--document-name", "test-issuer__statement.txt",
    "--published-at", "2026-04-09", "--cutoff", "2026-05-01",
    "--profile-name", "test-issuer-ratio/v1",
    "--currency", "RMB", "--unit", "MONETARY", "--scale", "MILLION",
]


class RatioAndSignFlip(unittest.TestCase):
    """The two shapes the pairing rule used to make unbuildable.

    A ratio reads two metrics in one period, which is the inverse of what a
    growth rate reads, and the rule was written for growth rates alone. And
    `sign` sat in the must-match set, which refused every question about a
    company that swung from profit to loss -- while protecting nothing, since
    each figure's sign is already checked against its own span.
    """

    def _build(self, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            document = root / "statement.txt"
            document.write_bytes(RATIO_DOCUMENT)
            output = root / "profile.json"
            done = subprocess.run(
                [sys.executable, str(BUILDER), "--document", str(document),
                 *RATIO_BASE, *extra, "--output", str(output)],
                capture_output=True, text=True,
                env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
            )
            done.profile = json.loads(output.read_text()) if output.exists() else None
            return done

    def test_a_margin_builds(self) -> None:
        """Gross profit over revenue, one period, two metrics."""

        done = self._build(
            "--question", "What was the issuer's gross margin in FY2025?",
            "--current-line", "Gross profit", "--current-value", "24490",
            "--current-period", "FY2025",
            "--comparison-line", "Total net revenues", "--comparison-value", "105919",
            "--comparison-period", "FY2025",
            "--metric", "gross_profit", "--comparison-metric", "revenue",
            "--operation", "ratio_percent",
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.profile["calculation"]["operation"], "ratio_percent")
        metrics = [e["normalized_semantics"]["metric"]
                   for e in done.profile["evidence_allowlist"]]
        self.assertEqual(sorted(metrics), ["gross_profit", "revenue"])
        self.assertEqual(done.profile["calculation"]["output_unit"], "PERCENT")

    def test_a_ratio_across_two_periods_is_refused(self) -> None:
        """A ratio of one metric in two periods is a growth rate wearing a hat."""

        done = self._build(
            "--question", "What was the issuer's gross margin in FY2025?",
            "--current-line", "Gross profit", "--current-value", "24490",
            "--current-period", "FY2025",
            "--comparison-line", "Total net revenues", "--comparison-value", "105919",
            "--comparison-period", "FY2024",
            "--metric", "gross_profit", "--comparison-metric", "revenue",
            "--operation", "ratio_percent",
        )
        self.assertNotEqual(done.returncode, 0)

    def test_a_growth_rate_within_one_period_is_still_refused(self) -> None:
        """The first shape is not loosened by the second existing."""

        done = self._build(
            "--question", "What was the growth in the issuer's gross profit?",
            "--current-line", "Gross profit", "--current-value", "24490",
            "--current-period", "FY2025",
            "--comparison-line", "Total net revenues", "--comparison-value", "105919",
            "--comparison-period", "FY2025",
            "--metric", "gross_profit", "--comparison-metric", "revenue",
            "--operation", "growth_rate_percent",
        )
        self.assertNotEqual(done.returncode, 0)

    def test_a_swing_from_profit_to_loss_builds(self) -> None:
        """The figure is printed in parentheses in one period and not the other."""

        done = self._build(
            "--question", "By how much did the issuer's operating result change in FY2025?",
            "--current-line", "Operating income (loss)", "--current-value", "-5823",
            "--current-period", "FY2025",
            "--comparison-line", "Operating income (loss)", "--comparison-value", "21270",
            "--comparison-period", "FY2024",
            "--metric", "operating_income", "--operation", "absolute_change",
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        signs = {e["normalized_semantics"]["sign"]
                 for e in done.profile["evidence_allowlist"]}
        self.assertEqual(signs, {"NEGATIVE", "POSITIVE"})
