from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from quant.evaluation.prediction import summarize_daily
from quant.state_evaluation import FUSIONS, RANK_ORDER, RISK_NAMES, StateEvaluator, risk_day, summarize_risk
from quant.state_report import render


def rank_report(years):
    days = []
    for year in years:
        for month in (1, 3):
            days.append({"as_of": f"{year}-{month:02d}-05T21:30:00+08:00", "year": year,
                "count": 8, "observed_count": 7, "unknown_count": 1,
                "rank_ic": None, "rank_ic_lower_bound": .02, "rank_ic_upper_bound": .04,
                "interval_mse": .08, "market_regime": "synthetic", "risk_fallback_to_G0_count": 0,
                "quantiles": [{"mean_forward_return": None} for _ in range(5)],
                "top_minus_bottom_forward_return": None, "fallback_count": 1,
                "effective_retrieval_count": 7, "mean_neighbor_date_count": 9., "mean_effective_dates": 8.})
    return summarize_daily(days)


def fixture():
    names = (*RANK_ORDER, *FUSIONS)
    risk = {}
    for year in range(2018, 2026):
        days = [risk_day(np.array([.01, .03, .04]), np.array([-.02, .1, np.nan]),
                         text=f"{year}-01-05", regime="synthetic", conditional_valid=np.array([True, True, False]))]
        report = summarize_risk(days)
        report.update(by_month={f"{year}-01": deepcopy(report)}, by_fixed_regime={"synthetic": deepcopy(report)})
        risk[str(year)] = {name: deepcopy(report) for name in (*RISK_NAMES, "selected-risk")}
    value = {"schema_version": "quant.state-comparison/v1", "full_original_scoring_pool": True,
        "new_blind_test": False, "selection": {"default_rank": "xgb-full", "selected_nn": "ordinary"},
        "development": {name: rank_report(range(2018, 2023)) for name in RANK_ORDER},
        "development_fusion": {name: rank_report(range(2018, 2023)) for name in FUSIONS},
        "check_2023": {name: rank_report((2023,)) for name in names},
        "final_2024_2025": {name: {"summary": rank_report((2024, 2025)), "paired_to_full_xgb": None} for name in names},
        "risk_by_year": risk, "risk_final": deepcopy(risk["2025"]), "gate_risk_increment": None,
        "risk_paired_mse_to_constant": {name: None for name in RISK_NAMES}}
    protocol = {"query_limit": 64, "candidate_limit": 1024, "k": 64, "date_cap": 8}
    schedule = {"all_base_cutoffs": ["synthetic"], "metric_candidate_attempt_upper_bound": 48,
                "final_geometry_refit_upper_bound": 4}
    return value, protocol, schedule


class StateReportTests(unittest.TestCase):
    def test_negative_result_and_all_year_month_coverage_are_kept_in_report(self):
        text = render(*fixture())
        self.assertIn("未获得相对原完整 XGB 的明确正增量支持", text)
        self.assertIn("不是亏损概率", text)
        self.assertIn("无条件回退成员日", text)
        for year in range(2018, 2026):
            self.assertIn(f"| {year} |", text)
            self.assertIn(f"| {year}-01 |", text)
        for name in (*RANK_ORDER, *FUSIONS, *RISK_NAMES):
            self.assertIn(name, text)

    def test_missing_final_candidate_or_year_cannot_be_called_complete(self):
        value, protocol, schedule = fixture()
        del value["final_2024_2025"]["G1"]
        with self.assertRaisesRegex(ValueError, "COMPLETE_RESULTS_REQUIRED"):
            render(value, protocol, schedule)
        value, _, _ = fixture()
        del value["risk_by_year"]["2021"]
        with self.assertRaisesRegex(ValueError, "COMPLETE_RESULTS_REQUIRED"):
            render(value, protocol, schedule)


    def test_missing_g1_diagnostic_cannot_be_reported_as_zero(self):
        value, protocol, schedule = fixture()
        del value["check_2023"]["G1"]["daily"][0]["risk_fallback_to_G0_count"]
        with self.assertRaisesRegex(ValueError, "STATE_REPORT_G1_DIAGNOSTIC_REQUIRED"):
            render(value, protocol, schedule)

    def test_old_g1_cache_requires_new_revision_and_preserves_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator = StateEvaluator.__new__(StateEvaluator)
            evaluator.root = root
            context = {"predictions": {"G1": "synthetic-frozen-prediction"}}
            evaluator.evaluation_context = lambda year, names, selected: context
            saved = rank_report((2023,))
            saved["input_binding"] = {**context, "selected_nn": None}
            del saved["daily"][0]["risk_fallback_to_G0_count"]
            path = root / "2023-G1.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "STATE_EVALUATION_G1_DIAGNOSTIC_REVISION_REQUIRED"):
                evaluator.annual_rank(2023, names=("G1",))
            self.assertEqual(path.read_bytes(), before)
            evaluator.root = root / "new-evaluation-revision"
            evaluator.root.mkdir()
            saved["daily"][0]["risk_fallback_to_G0_count"] = 1
            (evaluator.root / "2023-G1.json").write_text(json.dumps(saved), encoding="utf-8")
            restored = evaluator.annual_rank(2023, names=("G1",))
            self.assertEqual(restored["G1"]["daily"][0]["risk_fallback_to_G0_count"], 1)
            self.assertEqual(path.read_bytes(), before)


    def test_empty_g1_daily_series_cannot_be_reported_as_zero(self):
        value, protocol, schedule = fixture()
        for report in (value["development"]["G1"], value["check_2023"]["G1"],
                       value["final_2024_2025"]["G1"]["summary"]):
            report["daily"] = []
        with self.assertRaisesRegex(ValueError, "STATE_REPORT_G1_DIAGNOSTIC_REQUIRED"):
            render(value, protocol, schedule)

    def test_empty_g1_cache_requires_new_revision_and_preserves_bytes(self):
        with TemporaryDirectory() as directory:
            evaluator = StateEvaluator.__new__(StateEvaluator)
            evaluator.root = Path(directory)
            context = {"predictions": {"G1": "synthetic-frozen-prediction"}}
            evaluator.evaluation_context = lambda year, names, selected: context
            saved = rank_report((2023,))
            saved.update(daily=[], input_binding={**context, "selected_nn": None})
            path = evaluator.root / "2023-G1.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "STATE_EVALUATION_G1_DIAGNOSTIC_REVISION_REQUIRED"):
                evaluator.annual_rank(2023, names=("G1",))
            self.assertEqual(path.read_bytes(), before)
