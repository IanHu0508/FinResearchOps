from datetime import timedelta
import hashlib
import json
from pathlib import Path
import statistics
import unittest

from quant.contracts import ContractError, Prediction, session_of
from quant.evaluation.evidence import ValidationEvidence, validation_evidence
from quant.inference import build_signals
from quant.inference.note import research_note
from quant.labels.forward import label_rows_at_cutoff
from quant.labels.ranks import percentiles
from quant.pipeline import prepare_dataset
from quant.synthetic import make_synthetic_data

ROOT = Path(__file__).resolve().parents[2]


class StubModel:
    """Deterministic, dependency-free forecasts from two stock features."""

    view, ablation, artifact = "tabular", "stock+context", {}

    def __init__(self, model_version, training_cutoff):
        self.model_version, self.training_cutoff = model_version, training_cutoff

    def predict(self, rows):
        return tuple(Prediction(row.key, min(1.0, max(0.0, 0.5 + 3 * row.scalars[2] - row.scalars[7]))) for row in rows)


def group_differences(days, cutoff):
    """Independent recomputation of the complete-day top/bottom group differences."""
    tops, bottoms = [], []
    for predictions, labels in days:
        labels = label_rows_at_cutoff(labels, cutoff)
        if not all(label.supervised for label in labels):
            continue
        returns = [label.raw_return for label in labels]
        groups = {}
        for rank, value in zip(percentiles(tuple(p.predicted_target_percentile for p in predictions)), returns):
            groups.setdefault(min(int(rank * 5), 4), []).append(value)
        pool = statistics.fmean(returns)
        tops.append(statistics.fmean(groups[4]) - pool)
        bottoms.append(statistics.fmean(groups[0]) - pool)
    return len(tops), statistics.fmean(tops), statistics.fmean(bottoms)


class ResearchNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data, spec = make_synthetic_data()
        cls.dataset = prepare_dataset(data, spec)
        cls.rows, cls.labels = {}, {}
        for row in cls.dataset.panel.rows:
            cls.rows.setdefault(row.key.as_of, []).append(row)
        for label in cls.dataset.labels:
            cls.labels.setdefault(label.key.as_of, []).append(label)
        cls.dates = sorted(cls.rows)
        cls.as_of = cls.dates[90]
        cls.current = StubModel("stub-current", cls.dates[60] - timedelta(hours=1))
        cls.earlier = StubModel("stub-earlier", cls.dates[0] - timedelta(hours=1))
        cls.exact = validation_evidence(cls.days(cls.current, cls.dates[60:91]), cutoff=cls.as_of,
                                        scope="EXACT_MODEL", model_version="stub-current", label="当前年度模型")
        cls.family = validation_evidence(cls.days(cls.earlier, cls.dates[:60]), cutoff=cls.as_of,
                                         scope="METHOD_FAMILY", model_version="stub-earlier", label="上一年度模型")
        predictions = cls.current.predict(tuple(cls.rows[cls.as_of]))
        cls.signals = build_signals(predictions, cls.dataset.panel, as_ofs=(cls.as_of,), model=cls.current)

    @classmethod
    def days(cls, model, dates):
        return [(model.predict(tuple(cls.rows[d])), tuple(cls.labels[d])) for d in dates]

    def note(self, signal=None, evidence=None, **options):
        signal = signal or self.signals[1]
        row = next(r for r in self.rows[self.as_of] if r.key.symbol == signal["symbol"])
        return research_note(signal, row, evidence if evidence is not None else (self.exact, self.family),
                             mode=options.pop("mode", "HISTORICAL_SIMULATION"), **options)

    def test_evidence_uses_only_outcomes_known_at_the_cutoff(self):
        # Horizons end 20 sessions later: at dates[90] only forecasts through dates[69] are known.
        self.assertEqual((31, 10, 10), (self.exact.forecast_days, self.exact.evaluable_days, self.exact.complete_days))
        self.assertEqual((session_of(self.dates[60]).isoformat(), session_of(self.dates[69]).isoformat()),
                         self.exact.period)
        final = validation_evidence(self.days(self.current, self.dates[60:91]),
                                    cutoff=self.labels[self.as_of][0].knowledge_cutoff,
                                    scope="EXACT_MODEL", model_version="stub-current", label="当前年度模型")
        self.assertEqual(31, final.evaluable_days)
        self.assertNotEqual(self.exact.rank_ic_bounds, final.rank_ic_bounds)
        self.assertEqual((60, 60), (self.family.forecast_days, self.family.evaluable_days))

    def test_group_differences_are_relative_to_the_complete_pool(self):
        for evidence, model, dates in ((self.exact, self.current, self.dates[60:91]),
                                       (self.family, self.earlier, self.dates[:60])):
            days, top, bottom = group_differences(self.days(model, dates), self.as_of)
            self.assertEqual(days, evidence.complete_days)
            self.assertAlmostEqual(top, evidence.top_group_minus_pool, delta=1e-12)
            self.assertAlmostEqual(bottom, evidence.bottom_group_minus_pool, delta=1e-12)

    def test_nothing_matured_is_explicit_and_later_forecasts_are_refused(self):
        empty = validation_evidence(self.days(self.current, self.dates[60:61]), cutoff=self.dates[60],
                                    scope="EXACT_MODEL", model_version="stub-current", label="当前年度模型")
        self.assertEqual((1, 0, None, None, 0), (empty.forecast_days, empty.evaluable_days, empty.period,
                                                 empty.rank_ic_bounds, empty.complete_days))
        content = self.note(evidence=(empty,))["content"]
        self.assertIn("尚无已揭晓结果的样本外评价", content)
        self.assertIn("尚无可用于分组比较的结果完整评分日", content)
        with self.assertRaisesRegex(ContractError, "FORECAST_AFTER_EVIDENCE_CUTOFF"):
            validation_evidence(self.days(self.current, self.dates[90:92]), cutoff=self.as_of,
                                scope="EXACT_MODEL", model_version="stub-current", label="当前年度模型")
        for changes, reason in (({"scope": "OTHER"}, "EVIDENCE_SCOPE_INVALID"),
                                ({"period": None}, "EVIDENCE_PERIOD_INVALID"),
                                ({"complete_days": 11}, "EVIDENCE_COUNTS_INVALID")):
            values = {**self.exact.__dict__, **changes}
            with self.subTest(reason=reason), self.assertRaisesRegex(ContractError, reason):
                ValidationEvidence(**values)

    def test_note_is_a_deterministic_thesis_source_row(self):
        schema = json.loads((ROOT / "schemas/thesis-sources.v2.schema.json").read_text())["properties"]["sources"]["items"]
        row = self.note()
        self.assertEqual(set(schema["required"]), set(row))
        self.assertEqual(hashlib.sha256(row["content"].encode()).hexdigest(), row["sha256"])
        self.assertEqual(("QUANT", "research"), (row["id"], row["use"]))
        self.assertTrue(all(isinstance(row[k], str) and row[k].strip() for k in ("origin", "availability_note")))
        self.assertEqual(row, self.note())

    def test_note_shows_rank_with_direction_and_usage_rules_only(self):
        signal = next(s for s in self.signals
                      if f"{s['cross_sectional_model_rank']:.3f}" != f"{s['predicted_target_percentile']:.3f}")
        content = self.note(signal)["content"]
        self.assertIn(f"横截面排名分位：{signal['cross_sectional_model_rank']:.3f}", content)
        self.assertNotIn(f"{signal['predicted_target_percentile']:.3f}", content)
        for phrase in ("数值越高，模型给出的相对排序越靠前", "不是上涨概率、预期收益率、目标价或买卖建议",
                       "没有做归因分析", "不是本模型的检验结果，也不是该证券的胜率",
                       "最高20%组的20日平均参考收益相对全池平均", "不应以一方改写另一方", signal["inference_input_id"]):
            self.assertIn(phrase, content)

    def test_every_numeric_line_is_uniquely_quotable(self):
        content = self.note(extra_limitations=("开发期结果曾用于选择训练窗口，未作为验证证据列出。",))["content"]
        numeric = [line for line in content.splitlines() if any(c.isdigit() for c in line)]
        self.assertTrue(numeric)
        for line in numeric:
            self.assertGreaterEqual(len(line), 12, line)
            self.assertEqual(1, content.count(line), line)

    def test_simulation_refuses_later_evidence_and_retrospection_labels_it(self):
        later = validation_evidence(self.days(self.earlier, self.dates[:60]), cutoff=self.dates[100],
                                    scope="METHOD_FAMILY", model_version="stub-earlier", label="上一年度模型")
        with self.assertRaisesRegex(ContractError, "NOTE_EVIDENCE_AFTER_SCORING_TIME"):
            self.note(evidence=(self.exact, later))
        self.assertIn("回顾性", self.note(evidence=(self.exact, later), mode="RETROSPECTIVE")["content"])
        self.assertNotIn("回顾性", self.note()["content"])

    def test_evidence_scope_row_and_exact_record_are_enforced(self):
        relabelled = ValidationEvidence(**{**self.family.__dict__, "scope": "EXACT_MODEL"})
        for evidence, reason in (((relabelled,), "NOTE_EVIDENCE_SCOPE_MISMATCH"),
                                 ((self.family,), "NOTE_REQUIRES_ONE_EXACT_MODEL_RECORD"),
                                 ((self.exact, self.exact), "NOTE_REQUIRES_ONE_EXACT_MODEL_RECORD")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ContractError, reason):
                self.note(evidence=evidence)
        other = next(r for r in self.rows[self.as_of] if r.key.symbol != self.signals[1]["symbol"])
        with self.assertRaisesRegex(ContractError, "NOTE_ROW_SIGNAL_MISMATCH"):
            research_note(self.signals[1], other, (self.exact,), mode="HISTORICAL_SIMULATION")


if __name__ == "__main__":
    unittest.main()
