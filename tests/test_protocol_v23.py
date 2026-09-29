"""Protocol 23: every final report concludes, a fixed rule rates the scenarios, a stop still delivers.

Standard library only. These checks establish the rule, the schemas and the delivery
decision; they say nothing about whether any rating is right.
"""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from test_research_delivery import report
from finauditgate.adapters.thesis_format import schema_errors
from finauditgate.adapters.thesis_halted import conclusion, deliverable
from finauditgate.application.research_conclusion import RULE_NOTE, rating_text, rule_text
from finauditgate.application.research_delivery import normalize_report
from finauditgate.core.rating_rule import METHOD, rating_for, rule_rating


def results(*returns, dividend=False):
    return {"scenario_results": [{"scenario_id": f"F{i}", "return_ex_dividend": value,
                                  "return_with_dividend": value + 0.01 if dividend and value is not None else None}
                                 for i, value in enumerate(returns, 1)]}


class RatingRuleTest(unittest.TestCase):
    def test_bands_and_their_edges(self):
        for value, rating in ((0.20, "Buy"), (0.1999, "Overweight"), (0.05, "Overweight"), (0.0499, "Hold"),
                              (-0.0499, "Hold"), (-0.05, "Underweight"), (-0.1999, "Underweight"), (-0.20, "Sell")):
            with self.subTest(value=value):
                self.assertEqual(rating, rating_for(value))

    def test_equal_weights_prefer_the_return_with_dividends(self):
        rule = rule_rating(results(0.10, -0.40, dividend=True), 12)
        self.assertEqual(("COMPUTED", "Underweight", ["with_dividend", "with_dividend"], METHOD, -0.14, 12),
                         (rule["status"], rule["rating"], [r["basis"] for r in rule["scenario_returns"]], rule["method"],
                          rule["expected_return"], rule["horizon_months"]))
        self.assertEqual("Overweight", rule_rating(results(0.10, 0.00), 12)["rating"])
        self.assertEqual("ex_dividend", rule_rating(results(0.10), 12)["scenario_returns"][0]["basis"])

    def test_returns_are_annualized_over_the_horizon(self):
        five_years = rule_rating(results(0.4875), 60)
        self.assertEqual(("Overweight", 0.0827), (five_years["rating"], five_years["expected_return"]))
        self.assertEqual(0.4875, five_years["scenario_returns"][0]["return"])
        self.assertEqual(("Buy", 0.2544), (rule_rating(results(0.12), 6)["rating"], rule_rating(results(0.12), 6)["expected_return"]))
        self.assertEqual(-1.0, rule_rating(results(-1.0), 24)["expected_return"])

    def test_the_band_is_decided_at_the_precision_the_report_shows(self):
        # 3.00 -> 2.85 and 3.00 -> 3.60 are exact -5% and +20% moves that floating point computes just inside the bands.
        for moved, rating in ((2.85 / 3.00 - 1, "Underweight"), (3.60 / 3.00 - 1, "Buy")):
            with self.subTest(moved=moved):
                rule = rule_rating(results(moved), 12)
                self.assertEqual(rating, rule["rating"])
                self.assertIn(f"{rule['expected_return']:+.2%}", rule_text(rule))

    def test_a_missing_return_or_no_scenario_is_not_computable(self):
        missing = rule_rating(results(0.10, None), 12)
        self.assertEqual(("NOT_COMPUTABLE", "SCENARIO_RETURN_MISSING", None, None),
                         (missing["status"], missing["reason"], missing["rating"], missing["expected_return"]))
        self.assertEqual([None, None, None], [missing["scenario_returns"][1][k] for k in ("return", "basis", "annualized")])
        self.assertEqual("NO_SCENARIOS", rule_rating({"scenario_results": []}, 12)["reason"])

    def test_presentation(self):
        self.assertEqual("减持（Underweight）：2个情景回报等权平均-14.00%", rule_text(rule_rating(results(0.10, -0.40, dividend=True), 12)))
        self.assertEqual("增持（Overweight）：1个情景回报等权平均+8.27%（60个月期限年化）", rule_text(rule_rating(results(0.4875), 60)))
        self.assertIn("无法计算", rule_text(rule_rating(results(None), 12)))
        self.assertEqual("增持（Overweight），置信度低", rating_text("Overweight", "low"))
        self.assertIn("按研究期限换算为年化", RULE_NOTE)


class ConcludedSchemaTest(unittest.TestCase):
    def final(self, **fields):
        beliefs = [{"belief_id": f"D{i}", "explanation": {"text": "观点说明。"}} for i in (1, 2)]
        return {**deepcopy(report("结论。")), "belief_explanations": beliefs, "rating": "Hold", "confidence": "low", **fields}

    def test_the_final_report_concludes_with_a_confidence(self):
        self.assertEqual([], schema_errors("FinalResearchReport", self.final(), 23))
        self.assertIn(("enum", ("rating",)), {(e, p) for e, p, _ in schema_errors("FinalResearchReport", self.final(rating="REVIEW"), 23)})
        missing = self.final()
        del missing["confidence"]
        self.assertIn(("missing", ("confidence",)), {(e, p) for e, p, _ in schema_errors("FinalResearchReport", missing, 23)})
        # Before protocol 23 the same answer has an extra field and REVIEW is a rating.
        self.assertIn(("extra", ("confidence",)), {(e, p) for e, p, _ in schema_errors("FinalResearchReport", self.final(), 22)})
        old = self.final(rating="REVIEW")
        del old["confidence"]
        self.assertEqual([], schema_errors("FinalResearchReport", old, 22))

    def test_the_research_manager_concludes(self):
        value = {"plan": {"recommendation": "REVIEW", "rationale": "R", "strategic_actions": "S"},
                 "assessments": [{"claim_id": f"B{i}", "disposition": "use", "reason": "x"} for i in range(1, 5)],
                 "valuation_basis_and_gaps": "G"}
        self.assertEqual([], schema_errors("ResearchEvaluation", value, 22))
        self.assertEqual([("enum", ("plan", "recommendation"))],
                         [(e, p) for e, p, _ in schema_errors("ResearchEvaluation", value, 23)])
        value["plan"]["recommendation"] = "Underweight"
        self.assertEqual([], schema_errors("ResearchEvaluation", value, 23))

    def test_normalization_requires_a_rating_and_a_confidence(self):
        sources = {"sources": [{"id": "S01", "content": "x"}]}
        normalized = normalize_report(self.final(), sources, concluded=True)
        self.assertEqual(("Hold", "low"), (normalized["rating"], normalized["confidence"]))
        for broken in (self.final(rating="REVIEW"), self.final(confidence="certain")):
            with self.subTest(broken=(broken["rating"], broken["confidence"])), self.assertRaises(ValueError):
                normalize_report(broken, sources, concluded=True)
        with self.assertRaises(ValueError):
            normalize_report(self.final(), sources)


class RevisionCheckTest(unittest.TestCase):
    def test_a_kept_revision_covers_every_claim_and_states_each_revision(self):
        from finauditgate.application.thesis_case_v11 import check_revision
        revision = {"updates": [{"claim_id": "B1", "status": "revise", "updated_statement": "新表述"},
                                {"claim_id": "B2", "status": "maintain", "updated_statement": None}],
                    "counter_responses": [{"claim_id": "S1"}]}
        check_revision(revision, ["B1", "B2"], ["S1"])
        for mutate, reason in ((lambda r: r["updates"][0].update(updated_statement=None), "THESIS_REVISION_STATEMENT_INVALID"),
                               (lambda r: r["updates"][1].update(updated_statement="多余"), "THESIS_REVISION_STATEMENT_INVALID"),
                               (lambda r: r["updates"][1].update(claim_id="B1"), "THESIS_CLAIM_COVERAGE_INVALID"),
                               (lambda r: r["counter_responses"].clear(), "THESIS_CLAIM_COVERAGE_INVALID")):
            broken = deepcopy(revision)
            mutate(broken)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                check_revision(broken, ["B1", "B2"], ["S1"])


class HaltedDeliveryTest(unittest.TestCase):
    def session(self, protocol=23, halted=("Portfolio Manager", "FinalResearchReport", "JSONDecodeError")):
        value = None if halted is None else dict(zip(("node", "kind", "reason"), halted))
        return SimpleNamespace(protocol_version=protocol, recovery=SimpleNamespace(value={"halted": value}))

    def test_which_stops_are_delivered(self):
        self.assertTrue(deliverable(self.session(), ValueError("x")))
        self.assertTrue(deliverable(self.session(halted=("Trader", "ExecutionReview", "MODEL_SPEND_LIMIT")), ValueError("x")))
        for session, error in ((None, ValueError("x")), (self.session(protocol=22), ValueError("x")),
                               (self.session(), KeyboardInterrupt()), (self.session(halted=None), ValueError("x")),
                               (self.session(halted=("Bull Researcher", "InitialBrief", "THESIS_RESUME_INPUT_MISMATCH")), ValueError("x")),
                               (self.session(halted=("Bull Researcher", "InitialBrief", "THESIS_PENDING_RECOVERY_INPUT_MISMATCH")), ValueError("x")),
                               (self.session(halted=("Portfolio Manager", "FinalResearchReport", "KeyboardInterrupt")), ValueError("x"))):
            with self.subTest(session=session, error=error):
                self.assertFalse(deliverable(session, error))

    def test_the_conclusion_prefers_the_rule_then_the_research_manager(self):
        manager = {"plan": {"recommendation": "Hold"}}
        computed, blocked = rule_rating(results(0.30), 12), rule_rating(results(None), 12)
        self.assertEqual({"rating": "Buy", "source": "RULE"}, conclusion(computed, manager))
        self.assertEqual({"rating": "Hold", "source": "RESEARCH_MANAGER"}, conclusion(blocked, manager))
        self.assertEqual({"rating": "Hold", "source": "RESEARCH_MANAGER"}, conclusion(None, manager))
        self.assertEqual({"rating": None, "source": None}, conclusion(None, None))


if __name__ == "__main__":
    unittest.main()
