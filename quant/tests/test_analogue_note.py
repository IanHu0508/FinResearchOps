from copy import deepcopy
from dataclasses import replace
import hashlib
import unittest

from quant.contracts import ContractError
from quant.inference.note import analogue_research_note
from quant.tests import test_research_note as note_fixture


class AnalogueNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        note_fixture.ResearchNoteTests.setUpClass()
        cls.fixture = note_fixture.ResearchNoteTests

    def inputs(self):
        f = self.fixture
        signals = {name: {**f.signals[0], "model_version": prefix + "synthetic"}
                   for name, prefix in (("xgb", "xgboost-"), ("nn", "analogue-"), ("fusion", "fusion-"))}
        evidence = {name: (replace(f.exact, model_version=signal["model_version"]),) for name, signal in signals.items()}
        neighbors = [{"symbol": "SYN" + str(i % 4), "as_of": f.dates[i // 4].isoformat(),
                      "target_interval": [.2, .8], "weight": 1 / 32} for i in range(32)]
        details = {"alpha": .25, "k": 32, "fusion_weight": .5, "fallback": 0, "date_count": 8,
                   "mean_distance": .15, "neighbors": neighbors}
        return signals, f.rows[f.as_of][0], evidence, details

    def test_note_exposes_shared_inputs_neighbor_intervals_and_recomputed_rank_semantics(self):
        signals, row, evidence, details = self.inputs()
        note = analogue_research_note(signals, row, evidence, details, mode="HISTORICAL_SIMULATION")
        self.assertEqual(hashlib.sha256(note["content"].encode()).hexdigest(), note["sha256"])
        for expected in ("quant-research-note/v2", "不能计作三份独立市场证据", "不直接平均两个rank",
                         "[0.200000, 0.800000]", "不是胜率或校准可信度", "没有新的blind test"):
            self.assertIn(expected, note["content"])

    def test_model_pool_or_time_gated_evidence_mismatch_is_refused(self):
        signals, row, evidence, details = self.inputs()
        signals["nn"]["inference_input_id"] = "f" * 64
        with self.assertRaisesRegex(ContractError, "NOTE_V2_MODEL_POOL_MISMATCH"):
            analogue_research_note(signals, row, evidence, details, mode="HISTORICAL_SIMULATION")
        signals, row, evidence, details = self.inputs()
        evidence["nn"] = (replace(evidence["nn"][0], knowledge_cutoff=self.fixture.dates[-1]),)
        with self.assertRaisesRegex(ContractError, "NOTE_EVIDENCE_AFTER_SCORING_TIME"):
            analogue_research_note(signals, row, evidence, details, mode="HISTORICAL_SIMULATION")

    def test_neighbor_details_cannot_contradict_the_declared_retrieval_policy(self):
        for corruption, error in (("duplicate", "NOTE_V2_NEIGHBOR_UNIQUENESS"),
                                  ("date_cap", "NOTE_V2_NEIGHBOR_DATE_CAP"),
                                  ("date_count", "NOTE_V2_NEIGHBOR_DATE_COUNT"),
                                  ("weights", "NOTE_V2_NEIGHBOR_WEIGHTS"),
                                  ("fallback", "NOTE_V2_NEIGHBOR_COVERAGE")):
            with self.subTest(corruption=corruption):
                signals, row, evidence, details = self.inputs()
                if corruption == "duplicate":
                    details["neighbors"][1] = deepcopy(details["neighbors"][0])
                elif corruption == "date_cap":
                    for i, neighbor in enumerate(details["neighbors"]):
                        neighbor["symbol"] = "SYN" + str(i)
                    details["neighbors"][4]["as_of"] = details["neighbors"][0]["as_of"]
                elif corruption == "date_count":
                    details["date_count"] = 999
                elif corruption == "weights":
                    details["neighbors"][0]["weight"] += .01
                    details["neighbors"][1]["weight"] -= .01
                else:
                    details["fallback"] = 1
                with self.assertRaisesRegex(ContractError, error):
                    analogue_research_note(signals, row, evidence, details, mode="HISTORICAL_SIMULATION")

    def test_fusion_scalar_must_match_its_declared_weight_and_fallback(self):
        signals, row, evidence, details = self.inputs()
        signals["fusion"]["predicted_target_percentile"] = .01
        with self.assertRaisesRegex(ContractError, "NOTE_V2_FUSION_SCORE_MISMATCH"):
            analogue_research_note(signals, row, evidence, details, mode="HISTORICAL_SIMULATION")


if __name__ == "__main__":
    unittest.main()
