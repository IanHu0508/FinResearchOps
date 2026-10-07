import hashlib
import json
import unittest

from quant.state_packet import source_note
from quant.state_data import FEATURES


class StateNoteTests(unittest.TestCase):
    def test_saved_packet_reproduces_the_exact_source_note(self):
        value = {"symbol": "600000.SH", "as_of": "2025-01-05T21:30:00+08:00",
            "fit_cutoff": "2025-01-01T00:00:00+08:00", "original_pool_size": 100,
            "default_rank": "xgb-full", "rank_estimates": {}, "configurations": {},
            "inputs": dict.fromkeys(FEATURES, None), "mixtures": {}, "limitations": [],
            "risk": {"source": "constant", "conditional_available": False,
                     "score": None, "unconditional_fallback": .03}}
        configuration = {"eta": .2, "tau": 1., "date_count": 8, "effective_dates": 8.,
            "fallback": 0, "feature_weights": [1/14]*14,
            "group_weights": [5/14, 2/14, 3/14, 4/14], "neighbors": []}
        estimate = {"target_score": .4, "cross_sectional_percentile": .3}
        mixture = {"configuration_weights": [.4, .3, .3], "weighted_history": {}}
        sections = {
            "configurations": {"rank-numeric": configuration, "rank-mixed": configuration},
            "rank_estimates": {"xgb-full": estimate, "G1": estimate},
            "mixtures": {"G1": mixture, "G0": mixture},
        }
        for section, entries in sections.items():
            with self.subTest(section=section):
                packet = {**value, section: entries}
                restored = json.loads(json.dumps(packet, ensure_ascii=False, sort_keys=True))
                self.assertEqual(source_note(packet), source_note(restored))

    def test_risk_unavailability_is_visible_without_inventing_a_conditional_score(self):
        value = {"symbol": "600000.SH", "as_of": "2025-01-05T21:30:00+08:00",
                 "fit_cutoff": "2025-01-01T00:00:00+08:00", "original_pool_size": 100,
                 "default_rank": "xgb-full", "rank_estimates": {"xgb-full": {
                     "target_score": .5, "cross_sectional_percentile": .6}},
                 "risk": {"source": "constant", "conditional_available": False,
                          "score": None, "unconditional_fallback": .03}, "mixtures": {},
                 "inputs": dict.fromkeys(FEATURES, None), "configurations": {},
                 "limitations": ["合成检查，没有真实市场效果含义。"]}
        row = source_note(value)
        self.assertIn("条件风险不可用", row["content"])
        self.assertIn("G1采用G0", row["content"])
        self.assertIn("不是亏损概率或最大回撤", row["content"])
        self.assertEqual(["Market Analyst"], row["analyst_roles"])
        self.assertEqual(hashlib.sha256(row["content"].encode()).hexdigest(), row["sha256"])

    def test_distance_inputs_and_negative_neighbour_are_available_to_the_analyst(self):
        neighbor = {"date": "2023-01-04", "symbol": "600001.SH", "weight": .4,
                    "squared_distance": .2, "rank_interval": [.1, .2], "reference_return": -.05}
        value = {"symbol": "600000.SH", "as_of": "2025-01-05T21:30:00+08:00",
            "fit_cutoff": "2025-01-01T00:00:00+08:00", "original_pool_size": 100,
            "default_rank": "xgb-full", "rank_estimates": {},
            "inputs": {name: None if i == 0 else .01 for i, name in enumerate(FEATURES)},
            "configurations": {"risk-mixed": {"eta": .63, "tau": 2., "feature_weights": [1/14]*14,
                "group_weights": [5/14,2/14,3/14,4/14], "date_count": 20, "effective_dates": 12.5,
                "fallback": 0, "neighbors": [neighbor]}},
            "risk": {"source": "risk-mixed", "conditional_available": True, "score": .02,
                     "unconditional_fallback": None}, "mixtures": {}, "limitations": []}
        row = source_note(value)
        self.assertIn('"return_1":null', row["content"])
        self.assertIn('"reference_return":-0.05', row["content"])
        self.assertIn('"eta":0.63', row["content"])
        self.assertIn("600001.SH", row["content"])
        self.assertIn("非按收益筛选", row["content"])
        self.assertIn("exp(-D²/tau²)", row["content"])
