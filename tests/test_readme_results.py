"""The README first screen quotes headline research results; they must equal docs/status.md."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReadmeResultsTest(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.status = (ROOT / "docs/status.md").read_text(encoding="utf-8")

    def test_quoted_rank_ic_bounds_are_the_status_table_rounded(self):
        source = {(model, variant): (low, high) for model, variant, low, high in re.findall(
            r"^\| (Ridge|XGBoost|GRU) (stock-only|with context) \| \[([0-9.]+), ([0-9.]+)\] \|$", self.status, re.M)}
        quoted = re.findall(r"^\| (Ridge|XGBoost|GRU) \| ([0-9.]+)–([0-9.]+) \| ([0-9.]+)–([0-9.]+) \|$", self.readme, re.M)
        self.assertEqual(6, len(source))
        self.assertEqual(["Ridge", "XGBoost", "GRU"], [row[0] for row in quoted])
        for model, *values in quoted:
            expected = [f"{float(v):.4f}" for variant in ("stock-only", "with context") for v in source[model, variant]]
            self.assertEqual(expected, values, model)

    def test_quoted_scale_and_statements_are_recorded_in_status(self):
        for text in ("485 dates", "2,424,608 member-days", "All fitted models had positive annual mean IC bounds",
                     "Identification bounds are not confidence intervals or returns",
                     "exactly replayed before one-way performance reveal"):
            self.assertIn(text, self.status)
        for text in ("485 trading days", "2,424,608 stock-days", "All six models had positive annual mean IC bounds",
                     "docs/status.md#previous-slice-completed-fixed-research-study--2026-09-22"):
            self.assertIn(text, self.readme)
        self.assertIn("## Previous slice: completed fixed research study — 2026-09-22", self.status)


if __name__ == "__main__":
    unittest.main()
