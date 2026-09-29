"""Four analyst reports through the pinned graph with synthetic providers only.

These checks establish source routing, isolation and persistence; they do not
claim that an unavailable social sample proves neutral sentiment.
"""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_delivery import SelectionLLM
from test_thesis_quality_flow import QualityLLM
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters import thesis_analysts, thesis_correction, thesis_recovery
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.application.research_delivery import DeliveryContext
from finauditgate.core.artifacts import sha256_hex
from finauditgate.research import ResearchThesis
from native_support import payload_of


ANALYST_FIELDS = {
    "Fundamentals Analyst": "fundamentals_report",
    "Market Analyst": "market_report",
    "News Analyst": "news_report",
    "Sentiment Analyst": "sentiment_report",
}
ANALYSIS_MARKERS = {node: "SYNTHETIC_ANALYST_" + field.upper()
                    for node, field in ANALYST_FIELDS.items()}
QUANT_NOTE = "SYNTHETIC_QUANT_NOTE: next twenty-session cross-sectional ranking only; not annual return or a probability."
SOCIAL_UNAVAILABLE = "SYNTHETIC_SOCIAL_UNAVAILABLE: no eligible posts at the research cutoff; later posts were excluded."
HYPOTHESIS = "SYNTHETIC_HYPOTHESIS: test whether operating resilience persists."
CONSTRAINT = "SYNTHETIC_CONSTRAINT: distinguish the research horizon from the short-horizon ranking."
USER_VIEW = "SYNTHETIC_PRIVATE_DESIRED_BUY_MUST_NOT_REACH_MODELS"
MAINTAIN_RESTATEMENT = "SYNTHETIC_MAINTAIN_RESTATEMENT：原观点仍取决于经营条件是否持续；这是模型重述，不证明语义等价。"


def four_sources(*, social_unavailable=False):
    bundle = correction_sources()
    bundle["schema_version"] = "finresearchops.thesis-sources/v2"
    bundle["sources"][0]["use"] = "research"
    for source_id, origin, content in (
        ("NEWS", "SYNTHETIC_NEWS", "SYNTHETIC_NEWS_EVENT: a dated customer announcement; no profitability guarantee."),
        ("SOCIAL", "SYNTHETIC_SOCIAL", SOCIAL_UNAVAILABLE if social_unavailable else
         "SYNTHETIC_SOCIAL_POSTS: limited, conflicting opinions observed before the research cutoff; not a representative poll."),
        ("QUANT", "SYNTHETIC_QUANT", QUANT_NOTE),
    ):
        bundle["sources"].append({"id": source_id, "origin": origin, "use": "research",
            "availability_note": "SYNTHETIC_ONLY: frozen material as of 2026-03-02; not live data.",
            "content": content, "sha256": sha256_hex(content.encode())})
    return bundle


class FourAnalystLLM(SelectionLLM):
    omit_analysis_node: str | None = None
    invalid_source_node: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind != "AnalystReport":
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        payload = payload_of(messages[-1].content)
        node = payload["node"]
        self.requests.append({"node": node, "payload": deepcopy(payload), "schema": kind,
            "text": "\n".join(m.content for m in messages), "reasoning_effort": self.reasoning_effort,
            "structured_method": kwargs.get("synthetic_method")})
        refs = {"Fundamentals Analyst": ["S01"], "Market Analyst": ["QUANT"],
                "News Analyst": ["NEWS"], "Sentiment Analyst": ["SOCIAL"]}[node]
        unavailable = node == "Sentiment Analyst" and SOCIAL_UNAVAILABLE in json.dumps(payload)
        value = {"analysis": ANALYSIS_MARKERS[node] + ("；未取得合格帖子，不能由缺失判断中性或形成看多共识。" if unavailable else
                     "；保留事实、条件解释、反证与样本限制，本合成分析不提供真实投资建议。"),
            "observations": [] if unavailable else [{"statement": "合成资料支持有限观察，不证明持续性。", "evidence_refs": refs}],
            "coverage": "unavailable" if unavailable else "partial",
            "limits": ["SYNTHETIC_ONLY：来源与样本范围有限，不能承诺收益或假造未取得数据。"],
            "evidence_refs": refs}
        if node == self.omit_analysis_node:
            del value["analysis"]
        if node == self.invalid_source_node:
            value["evidence_refs"] = ["SYNTHETIC_MISSING_SOURCE"]
        return self._result(json.dumps(value, ensure_ascii=False))


class FourAnalystQualityLLM(FourAnalystLLM, QualityLLM):
    """Use the same synthetic main chain plus bounded quality responses."""


def synthetic_limits(count):
    return [f"SYNTHETIC_LIMIT_{i}: 保留原文的范围说明。" for i in range(count)]


class OverlongAnalystLimitsLLM(FourAnalystLLM):
    limits_count: int = 13
    limit_problem: str | None = None
    extra_observations: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        payload = payload_of(messages[-1].content)
        if not schema or schema.__name__ != "AnalystReport" or payload["node"] != "News Analyst":
            return result
        value = json.loads(result.generations[0].message.content)
        value["limits"] = synthetic_limits(self.limits_count)
        if self.limit_problem == "blank":
            value["limits"][-1] = "   "
        elif self.limit_problem == "nonstring":
            value["limits"][-1] = 7
        elif self.limit_problem == "not_list":
            value["limits"] = {"text": "SYNTHETIC_WRONG_CONTAINER"}
        if self.extra_observations:
            value["observations"] = value["observations"] * 13
        return self._result(json.dumps(value, ensure_ascii=False))


CITATION_NOTE = "SYNTHETIC_CITATION_NOTE：本项中的程序辅助计算未经人工复核。"


class CitationNoteLLM(FourAnalystLLM):
    note: object = CITATION_NOTE

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "InitialBrief" or self.requests[-1]["node"] != "Bear Researcher":
            return result
        value = json.loads(result.generations[0].message.content)
        value["claims"][1]["evidence_refs_note"] = self.note
        return self._result(json.dumps(value, ensure_ascii=False))


class MissingCloserLLM(FourAnalystLLM):
    dangling_comma: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "RiskBrief" or self.requests[-1]["node"] != "Neutral Analyst":
            return result
        text = result.generations[0].message.content
        assert text.endswith("]}")
        return self._result(text[:-2] + (",}" if self.dangling_comma else "}"))


class PendingAcrossSectionsLLM(FourAnalystLLM):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "FinalResearchReport":
            return result
        value = json.loads(result.generations[0].message.content)
        value["financial_analysis"]["operating_performance"]["text"] += "需跟踪DDR5进展。"
        value["financial_analysis"]["cash_and_capital_allocation"]["text"] += "需核对前20大客户。"
        return self._result(json.dumps(value, ensure_ascii=False))


class RestatedBeliefLLM(FourAnalystLLM):
    belief_problem: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "ForwardRevision":
            return result
        value = json.loads(result.generations[0].message.content)
        value["belief_updates"][1]["new_statement"] = MAINTAIN_RESTATEMENT
        if self.belief_problem == "revise_without_statement":
            value["belief_updates"][0]["new_statement"] = None
        elif self.belief_problem == "missing_belief":
            value["belief_updates"].pop()
        elif self.belief_problem == "wrong_parameter":
            value["changes"][0]["expected_before"]["amount"] = 999.0
        elif self.belief_problem in ("withdraw", "unresolved"):
            value["belief_updates"][1]["status"] = self.belief_problem
        return self._result(json.dumps(value, ensure_ascii=False))


class LabeledFinalLLM(FourAnalystLLM):
    with_unbound_amount: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "FinalResearchReport":
            return result
        value = json.loads(result.generations[0].message.content)
        value["summary"]["text"] += " FY2024的历史资料仍需核对。"
        if self.with_unbound_amount:
            value["summary"]["text"] += "EPS为999元。"
        return self._result(json.dumps(value, ensure_ascii=False))


class FourAnalystFlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model=None, *, root=None, sources=None, protocol=17, resume=None, review=False):
        model = model or FourAnalystLLM()
        app = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(
            protocol_version=protocol, resume_from=resume))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "四维分析后形成可复核研究。",
            sources=four_sources() if sources is None else sources, review=review,
            hypotheses=(HYPOTHESIS,), research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        with patched_native_runtime(model), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_stocktwits_messages",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_reddit_posts",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")):
            view = app.handle(command)
        return view, app, model

    def test_four_reports_run_in_order_and_persist_without_optional_review(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual("finresearchops.thesis-case/v17", record["schema_version"])
        self.assertEqual(list(ANALYST_FIELDS), [r["node"] for r in model.requests[:4]])
        self.assertEqual(["AnalystReport"] * 4, [r["schema"] for r in model.requests[:4]])
        self.assertEqual(17, len(record["model_calls"]))
        self.assertEqual(17, len(record["exchanges"]))
        self.assertEqual(17, len(model.requests))
        self.assertEqual("DEFERRED", view.review["status"])
        self.assertEqual([], record["tool_calls"])
        self.assertEqual(set(ANALYST_FIELDS), set(record["analyst_reports"]))
        nodes = set(record["topology"]["nodes"])
        self.assertTrue(set(ANALYST_FIELDS) <= nodes)
        self.assertTrue({"tools_news", "tools_social", "Msg Clear News", "Msg Clear Sentiment"} <= nodes)
        edges = {(e["source"], e["target"]) for e in record["topology"]["edges"]}
        self.assertTrue({("__start__", "Fundamentals Analyst"), ("Msg Clear Fundamentals", "Market Analyst"),
                         ("Msg Clear Market", "News Analyst"), ("Msg Clear News", "Sentiment Analyst"),
                         ("Msg Clear Sentiment", "Bull Researcher")} <= edges)
        appendix = Path(view.report_path).with_name("process-record.md").read_text()
        main = Path(view.report_path).read_text()
        for node, field in ANALYST_FIELDS.items():
            self.assertIn(ANALYSIS_MARKERS[node], record["reports"][field])
            self.assertIn(ANALYSIS_MARKERS[node], main + appendix)
            self.assertEqual(record["analyst_reports"][node], record["exchanges"][list(ANALYST_FIELDS).index(node)]["parsed"])
        self.assertIn(QUANT_NOTE, main + appendix)
        self.assertIn("| 1 | Fundamentals Analyst | AnalystReport | 已提供 | 未见显式引用 |", main)
        self.assertIn("| 2 | Market Analyst | AnalystReport | 已提供 | 有显式引用 |", main)
        self.assertIn("| 14 | Portfolio Manager | IndependentAssessment | 已提供 | 未见显式引用 |", main)
        self.assertIn("| 17 | Portfolio Manager | FinalResearchReport | 已提供 | 未见显式引用 |", main)
        self.assertIn(HYPOTHESIS, main + appendix)
        self.assertIn(CONSTRAINT, main + appendix)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_real_payloads_preserve_independence_and_user_conclusion_isolation(self):
        view, _, model = self.run_case()
        reports = view.latest_report["analyst_reports"]
        for call in model.requests[:4]:
            self.assertNotIn("analyst_reports", call["payload"])
            for marker in ANALYSIS_MARKERS.values():
                self.assertNotIn(marker, json.dumps(call["payload"]))
        for call in model.requests[4:]:
            if call["schema"] == "IndependentAssessment":
                self.assertNotIn("analyst_reports", call["payload"])
                for marker in ANALYSIS_MARKERS.values():
                    self.assertNotIn(marker, json.dumps(call["payload"]))
            else:
                self.assertEqual(reports, call["payload"]["analyst_reports"])
        for call in model.requests:
            self.assertNotIn(USER_VIEW, call["text"])
            self.assertNotIn("user_view", call["payload"]["request"])
            self.assertEqual([HYPOTHESIS], call["payload"]["request"]["hypotheses"])
            self.assertEqual([CONSTRAINT], call["payload"]["request"]["research_constraints"])
            self.assertIn(QUANT_NOTE, json.dumps(call["payload"]))
        self.assertEqual(USER_VIEW, view.latest_report["request"]["user_view"])

    def test_four_report_case_reopens_with_standard_library_only(self):
        view, _, _ = self.run_case()
        repo = Path(__file__).parents[1]
        script = (
            "import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
            "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
            "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v17';"
            "assert len(v.latest_report['analyst_reports'])==4;"
            "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))"
        )
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script,
            str(self.root), view.case_ref], env={**os.environ, "PYTHONPATH": str(repo / "src")},
            capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_resume_reuses_all_seventeen_calls_and_refuses_cross_version(self):
        view, _, _ = self.run_case(root=self.root / "original")
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        original = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        main_bytes = Path(view.report_path).read_bytes()
        repeated, app, model = self.run_case(root=self.root / "resumed", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual(17, repeated.latest_report["reused_calls"]["used_calls"])
        schema = json.loads((Path(__file__).parents[1] / "schemas/thesis-case.v17.schema.json").read_text())
        reuse_schema = schema["properties"]["reused_calls"]
        reused = repeated.latest_report["reused_calls"]
        self.assertIs(False, reuse_schema["additionalProperties"])
        self.assertTrue(set(reuse_schema["required"]) <= set(reused))
        self.assertTrue(set(reused) <= set(reuse_schema["properties"]))
        self.assertEqual(reuse_schema["properties"]["budget_origin"]["const"], reused["budget_origin"])
        self.assertEqual(view.latest_report["model_calls"], repeated.latest_report["model_calls"])
        self.assertEqual(view.latest_report["analyst_reports"], repeated.latest_report["analyst_reports"])
        self.assertEqual(repeated, app.read_case(repeated.case_ref))
        self.assertEqual(original, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(main_bytes, Path(view.report_path).read_bytes())
        request = json.loads((execution / "request.json").read_bytes())
        with self.assertRaises(ValueError):
            CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=16)
        _, _, _ = self.run_case(SelectionLLM(), root=self.root / "v16", protocol=16)
        old_execution = next((self.root / "v16").glob("application/thesis-executions/*"))
        old_request = json.loads((old_execution / "request.json").read_bytes())
        with self.assertRaises(ValueError):
            CompletedCalls(old_execution, old_request["request"], old_request["sources"], "deepseek-flash", protocol_version=17)

    def test_optional_quality_review_retains_complete_four_analyst_delivery(self):
        view, app, model = self.run_case(FourAnalystQualityLLM(), review=True)
        self.assertEqual(19, len(model.requests))
        self.assertEqual(17, len(view.latest_report["model_calls"]))
        self.assertEqual(2, len(view.review["model_calls"]))
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual("quality-report.md", Path(view.delivery_report_path).name)
        report = Path(view.delivery_report_path).read_text()
        appendix = Path(view.delivery_report_path).with_name("quality-process-record.md").read_text()
        for name, text in (("quality-report.md", report), ("quality-process-record.md", appendix)):
            for marker in ANALYSIS_MARKERS.values():
                self.assertTrue(marker in text, f"{name} omitted {marker}")
            for supplied in (QUANT_NOTE, HYPOTHESIS, CONSTRAINT):
                self.assertTrue(supplied in text, f"{name} omitted {supplied}")
        self.assertIn("| 2 | Market Analyst | AnalystReport | 已提供 | 有显式引用 |", report)
        self.assertEqual(view, app.read_case(view.case_ref))
        repo = Path(__file__).parents[1]
        code = (
            "import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
            "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
            "assert v.review['status']=='COMPLETED';"
            "assert Path(v.delivery_report_path).name=='quality-report.md';"
            "assert len(v.latest_report['analyst_reports'])==4;"
            "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))"
        )
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", code,
            str(self.root), view.case_ref], env={**os.environ, "PYTHONPATH": str(repo / "src")},
            capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_changed_or_omitted_analyst_outputs_are_rejected(self):
        view, app, _ = self.run_case()
        for kind in ("parsed", "missing", "rendered"):
            with self.subTest(kind=kind):
                forged = deepcopy(view.latest_report)
                if kind == "parsed":
                    forged["analyst_reports"]["News Analyst"]["analysis"] = "SYNTHETIC_FORGED_ANALYSIS"
                elif kind == "missing":
                    del forged["analyst_reports"]["News Analyst"]
                else:
                    forged["reports"]["news_report"] = "SYNTHETIC_FORGED_RENDERED_REPORT"
                with self.assertRaises(ValueError):
                    validate(forged)
        path = Path(view.report_path).with_name("process-record.md")
        original = path.read_bytes()
        path.write_bytes(original + b"SYNTHETIC_APPENDIX_TAMPER")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)

    def test_unavailable_historical_social_sample_is_retained_without_fabricated_observations(self):
        view, app, _ = self.run_case(sources=four_sources(social_unavailable=True))
        social = view.latest_report["analyst_reports"]["Sentiment Analyst"]
        self.assertEqual("unavailable", social["coverage"])
        self.assertEqual([], social["observations"])
        self.assertIn("不能由缺失判断中性", social["analysis"])
        self.assertIn("未取得合格帖子", view.latest_report["reports"]["sentiment_report"])
        self.assertEqual(17, len(view.latest_report["model_calls"]))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_missing_analysis_or_unknown_source_cannot_become_a_complete_report(self):
        for mode in ("missing", "source"):
            with self.subTest(mode=mode):
                model = FourAnalystLLM(**({"omit_analysis_node": "News Analyst"} if mode == "missing" else
                                         {"invalid_source_node": "News Analyst"}))
                root = self.root / mode
                with self.assertRaises(ApplicationError):
                    self.run_case(model, root=root)
                self.assertFalse(list(root.glob("application/thesis-cases/*/case.json")))

    def test_v17_refuses_live_vendor_mode_before_analyst_or_data_calls(self):
        model = FourAnalystLLM()
        app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher(protocol_version=17))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "No unapproved live prefetch.", review=False)
        with patched_native_runtime(model), self.assertRaises((ApplicationError, ValueError)):
            app.handle(command)
        self.assertEqual([], model.requests)
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def test_overlong_analyst_limits_preserve_every_raw_item_without_new_generation(self):
        for count in (12, 13, 48):
            with self.subTest(count=count):
                view, app, model = self.run_case(OverlongAnalystLimitsLLM(limits_count=count),
                                                  root=self.root / str(count))
                raw = json.loads(view.latest_report["model_calls"][2]["output"][0]["content"])
                expected = synthetic_limits(count)
                self.assertEqual(expected, raw["limits"])
                normalized = view.latest_report["analyst_reports"]["News Analyst"]
                self.assertEqual(expected if count == 12 else expected[:11] + ["\n".join(expected[11:])],
                                 normalized["limits"])
                self.assertEqual({k: v for k, v in raw.items() if k != "limits"},
                                 {k: v for k, v in normalized.items() if k != "limits"})
                for item in expected:
                    self.assertIn(item, view.latest_report["reports"]["news_report"])
                self.assertEqual(17, len(model.requests))
                self.assertEqual([], view.latest_report["recovery"]["attempts"])
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_limit_join_does_not_relax_other_shapes_or_content_validation(self):
        cases = ({"limits_count": 49}, {"limit_problem": "blank"},
                 {"limit_problem": "nonstring"}, {"limit_problem": "not_list"},
                 {"extra_observations": True}, {"omit_analysis_node": "News Analyst"},
                 {"invalid_source_node": "News Analyst"})
        for index, arguments in enumerate(cases):
            with self.subTest(arguments=arguments):
                root = self.root / str(index)
                with self.assertRaises(ApplicationError):
                    self.run_case(OverlongAnalystLimitsLLM(**arguments), root=root)
                self.assertFalse(list(root.glob("application/thesis-cases/*/case.json")))

    def test_captured_overlong_limits_replay_reuses_three_paid_answers_and_budget(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "保留已返回的分析原文与费用。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)

        def make_budget():
            return ModelBudget(ceiling_cny="100", max_calls=24,
                               max_input_bytes=524288, max_output_tokens=65536)

        old_root = self.root / "old-halt"
        old_budget = make_budget()
        old_model = OverlongAnalystLimitsLLM()
        old_app = FinResearchOps(artifact_root=old_root, researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=old_budget))
        with patched_native_runtime(old_model), patch.object(thesis_analysts, "normalize_limits", deepcopy), \
                patch("finauditgate.adapters.model_http.model_http_client",
                      return_value=SimpleNamespace(close=lambda: None)), self.assertRaises(ApplicationError):
            old_app.handle(command)
        self.assertEqual(3, old_budget.calls)
        execution = next(old_root.glob("application/thesis-executions/*"))
        old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(old_files[Path("runtime-receipt.json")])
        self.assertEqual({"node": "News Analyst", "kind": "AnalystReport", "reason": "ValidationError"},
                         runtime["recovery"]["halted"])
        self.assertEqual(13, len(json.loads(runtime["model_calls"][2]["output"][0]["content"])["limits"]))
        new_budget = make_budget()
        model = FourAnalystLLM()
        app = FinResearchOps(artifact_root=self.root / "resumed", researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=new_budget, resume_from=execution,
            replay_presentation_failure=True))
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        self.assertEqual(14, len(model.requests))
        self.assertEqual("Sentiment Analyst", model.requests[0]["node"])
        self.assertEqual(17, new_budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], new_budget.receipt()["usage"][:3])
        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"][:3])
        self.assertEqual(3, view.latest_report["reused_calls"]["used_calls"])
        replay = view.latest_report["reused_calls"]["presentation_replay"]
        self.assertEqual(runtime["recovery"]["halted"], replay["original_halt"])
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertEqual(synthetic_limits(13)[:11] + ["\n".join(synthetic_limits(13)[11:])],
                         view.latest_report["analyst_reports"]["News Analyst"]["limits"])
        self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_text_citation_note_stays_in_its_claim_without_extra_calls(self):
        view, app, model = self.run_case(CitationNoteLLM())
        record = view.latest_report
        self.assertEqual(17, len(model.requests))
        self.assertEqual([], record["recovery"]["attempts"])
        brief = record["exchanges"][5]
        self.assertEqual(("Bear Researcher", "InitialBrief"), (brief["node"], brief["kind"]))
        self.assertEqual("Synthetic assumptions.\n模型引用说明：" + CITATION_NOTE,
                         brief["parsed"]["claims"][1]["uncertainty"])
        self.assertNotIn("evidence_refs_note", brief["parsed"]["claims"][1])
        raw = json.loads(record["model_calls"][5]["output"][0]["content"])
        self.assertEqual(CITATION_NOTE, raw["claims"][1]["evidence_refs_note"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_captured_citation_note_replay_reuses_six_paid_answers_and_budget(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "保留已返回的论点原文与费用。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        offline_http = patch("finauditgate.adapters.model_http.model_http_client",
                             return_value=SimpleNamespace(close=lambda: None))

        def make_budget():
            return ModelBudget(ceiling_cny="100", max_calls=24,
                               max_input_bytes=524288, max_output_tokens=65536)

        def halted_run(root, model):
            budget = make_budget()
            app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
                protocol_version=17, live=True, budget=budget))
            # The previous rule kept every non-null note, so the strict schema refused it.
            with patched_native_runtime(model), offline_http, self.assertRaises(ApplicationError), \
                    patch.object(thesis_recovery, "normalize_role", lambda value, kind: deepcopy(value)):
                app.handle(command)
            return budget, next(root.glob("application/thesis-executions/*"))

        def resume(root, execution, budget, model):
            app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
                protocol_version=17, live=True, budget=budget, resume_from=execution,
                replay_presentation_failure=True))
            with patched_native_runtime(model), offline_http:
                return app.handle(command), app

        old_budget, execution = halted_run(self.root / "old-halt", CitationNoteLLM())
        self.assertEqual(6, old_budget.calls)
        old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(old_files[Path("runtime-receipt.json")])
        self.assertEqual({"node": "Bear Researcher", "kind": "InitialBrief", "reason": "ValidationError"},
                         runtime["recovery"]["halted"])
        new_budget, model = make_budget(), FourAnalystLLM()
        view, app = resume(self.root / "resumed", execution, new_budget, model)
        self.assertEqual(11, len(model.requests))
        self.assertEqual(("Bull Researcher", "RevisionBrief"), (model.requests[0]["node"], model.requests[0]["schema"]))
        self.assertEqual(17, new_budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], new_budget.receipt()["usage"][:6])
        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"][:6])
        self.assertEqual(6, view.latest_report["reused_calls"]["used_calls"])
        replay = view.latest_report["reused_calls"]["presentation_replay"]
        self.assertEqual(runtime["recovery"]["halted"], replay["original_halt"])
        self.assertEqual("CAPTURED_CITATION_NOTE_MERGED_WITHOUT_TEXT_LOSS", replay["validation"])
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertTrue(view.latest_report["exchanges"][5]["parsed"]["claims"][1]["uncertainty"].endswith(
            "\n模型引用说明：" + CITATION_NOTE))
        self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

        _, refused = halted_run(self.root / "structured-note", CitationNoteLLM(note={"text": CITATION_NOTE}))
        model = FourAnalystLLM()
        with self.assertRaises(ApplicationError):
            resume(self.root / "structured-resumed", refused, make_budget(), model)
        self.assertEqual([], model.requests)

    def test_missing_trailing_bracket_is_closed_without_changing_text(self):
        view, app, model = self.run_case(MissingCloserLLM())
        record = view.latest_report
        self.assertEqual(17, len(model.requests))
        self.assertEqual([], record["recovery"]["attempts"])
        raw = record["model_calls"][12]["output"][0]["content"]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        brief = record["exchanges"][12]
        self.assertEqual(("Neutral Analyst", "RiskBrief"), (brief["node"], brief["kind"]))
        self.assertEqual(json.loads(raw[:-1] + "]}"), {k: brief["parsed"][k] for k in json.loads(raw[:-1] + "]}")})
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_captured_missing_bracket_replay_reuses_thirteen_paid_answers(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "保留已返回的风险原文与费用。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        offline_http = patch("finauditgate.adapters.model_http.model_http_client",
                             return_value=SimpleNamespace(close=lambda: None))

        def make_budget():
            return ModelBudget(ceiling_cny="100", max_calls=24,
                               max_input_bytes=524288, max_output_tokens=65536)

        def halted_run(root, model):
            budget = make_budget()
            app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
                protocol_version=17, live=True, budget=budget))
            # The previous parser refused any malformed JSON.
            with patched_native_runtime(model), offline_http, self.assertRaises(ApplicationError), \
                    patch.object(thesis_recovery, "close_json_tail", lambda text: None):
                app.handle(command)
            return budget, next(root.glob("application/thesis-executions/*"))

        def resume(root, execution, budget, model):
            app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
                protocol_version=17, live=True, budget=budget, resume_from=execution,
                replay_presentation_failure=True))
            with patched_native_runtime(model), offline_http:
                return app.handle(command), app

        old_budget, execution = halted_run(self.root / "old-halt", MissingCloserLLM())
        self.assertEqual(13, old_budget.calls)
        old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(old_files[Path("runtime-receipt.json")])
        self.assertEqual({"node": "Neutral Analyst", "kind": "RiskBrief", "reason": "JSONDecodeError"},
                         runtime["recovery"]["halted"])
        new_budget, model = make_budget(), FourAnalystLLM()
        view, app = resume(self.root / "resumed", execution, new_budget, model)
        self.assertEqual(4, len(model.requests))
        self.assertEqual(("Portfolio Manager", "IndependentAssessment"), (model.requests[0]["node"], model.requests[0]["schema"]))
        self.assertEqual(17, new_budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], new_budget.receipt()["usage"][:13])
        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"][:13])
        self.assertEqual(13, view.latest_report["reused_calls"]["used_calls"])
        replay = view.latest_report["reused_calls"]["presentation_replay"]
        self.assertEqual(runtime["recovery"]["halted"], replay["original_halt"])
        self.assertEqual("CAPTURED_JSON_TAIL_CLOSED_WITHOUT_TEXT_CHANGE", replay["validation"])
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

        _, refused = halted_run(self.root / "dangling", MissingCloserLLM(dangling_comma=True))
        model = FourAnalystLLM()
        with self.assertRaises(ApplicationError):
            resume(self.root / "dangling-resumed", refused, make_budget(), model)
        self.assertEqual([], model.requests)

    def test_pending_numbers_in_several_sections_save_and_reopen(self):
        view, app, model = self.run_case(PendingAcrossSectionsLLM())
        check = view.latest_report["evidence_check"]
        self.assertEqual("PARTIAL", view.status)
        self.assertEqual([("financial_analysis.cash_and_capital_allocation", "20"), ("financial_analysis.operating_performance", "DDR5")],
                         [(r["field"], r["reference"]) for r in check["findings"]])
        self.assertEqual(17, len(model.requests))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_captured_final_halted_by_finding_order_replays_without_new_calls(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "保留已返回的终稿与费用。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        offline_http = patch("finauditgate.adapters.model_http.model_http_client",
                             return_value=SimpleNamespace(close=lambda: None))
        make_budget = lambda: ModelBudget(ceiling_cny="100", max_calls=24, max_input_bytes=524288, max_output_tokens=65536)
        original, calls = DeliveryContext.evidence_check, []

        def generation_order(self):
            # The earlier generation listed findings in schema order, unlike the saved canonical record.
            value = original(self)
            calls.append(1)
            if len(calls) == 1:
                value["findings"] = list(reversed(value["findings"]))
            return value

        old_budget = make_budget()
        old_root = self.root / "old-halt"
        old_app = FinResearchOps(artifact_root=old_root, researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=old_budget))
        with patched_native_runtime(PendingAcrossSectionsLLM()), offline_http, self.assertRaises(ApplicationError), \
                patch.object(DeliveryContext, "evidence_check", generation_order):
            old_app.handle(command)
        execution = next(old_root.glob("application/thesis-executions/*"))
        old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(old_files[Path("runtime-receipt.json")])
        self.assertEqual({"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": "THESIS_EVIDENCE_CHECK_CHANGED"},
                         runtime["recovery"]["halted"])
        self.assertEqual(17, old_budget.calls)
        new_budget, model = make_budget(), FourAnalystLLM()
        app = FinResearchOps(artifact_root=self.root / "resumed", researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=new_budget, resume_from=execution, replay_presentation_failure=True))
        with patched_native_runtime(model), offline_http:
            view = app.handle(command)
        self.assertEqual([], model.requests)
        self.assertEqual(17, new_budget.calls)
        self.assertEqual(17, view.latest_report["reused_calls"]["used_calls"])
        self.assertEqual("COMPLETE_CAPTURED_FINAL_REVALIDATED", view.latest_report["reused_calls"]["presentation_replay"]["validation"])
        self.assertEqual("PARTIAL", view.status)
        self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_maintain_restatement_is_preserved_without_changing_financial_values(self):
        control, _, _ = self.run_case(root=self.root / "control")
        view, app, model = self.run_case(RestatedBeliefLLM(), root=self.root / "restatement")
        record = view.latest_report
        revised = record["forward_revision"]["belief_updates"][1]
        self.assertEqual("maintain", revised["status"])
        self.assertEqual(MAINTAIN_RESTATEMENT, revised["new_statement"])
        raw = json.loads(record["model_calls"][15]["output"][0]["content"])
        self.assertEqual(raw["belief_updates"], record["forward_revision"]["belief_updates"])
        for key in ("forward_draft", "forward_calculations", "effective_forward_draft",
                    "effective_forward_calculations", "applied_changes"):
            self.assertEqual(control.latest_report[key], record[key])
        original_belief = record["independent_assessment"]["beliefs"][1]["statement"]
        report = Path(view.report_path).read_text()
        appendix = Path(view.report_path).with_name("process-record.md").read_text()
        for statement in (original_belief, MAINTAIN_RESTATEMENT):
            self.assertTrue(statement in report + appendix, "A preserved belief statement was omitted")
        self.assertIn("模型标记：维持", report)
        self.assertIn("未认定与原句同义", report)
        self.assertEqual(17, len(model.requests))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_maintain_restatement_does_not_relax_other_rules_or_v16(self):
        for problem in ("revise_without_statement", "missing_belief", "wrong_parameter", "withdraw", "unresolved"):
            with self.subTest(problem=problem):
                root = self.root / problem
                with self.assertRaises(ApplicationError):
                    self.run_case(RestatedBeliefLLM(belief_problem=problem), root=root)
                self.assertFalse(list(root.glob("application/thesis-cases/*/case.json")))
        with self.assertRaises(ApplicationError):
            self.run_case(RestatedBeliefLLM(), root=self.root / "v16", protocol=16)

    def test_captured_maintain_restatement_replay_only_adds_one_paid_final_response(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "观点文字原样保留，参数与费用不重做。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)

        def make_budget():
            return ModelBudget(ceiling_cny="100", max_calls=24,
                               max_input_bytes=524288, max_output_tokens=65536)

        current_check = thesis_correction.valid_belief_updates

        def old_check(updates, *, allow_maintain_restatement=False):
            return current_check(updates, allow_maintain_restatement=False)

        old_root = self.root / "old-belief-halt"
        old_budget = make_budget()
        old_model = RestatedBeliefLLM()
        old_app = FinResearchOps(artifact_root=old_root, researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=old_budget))
        with patched_native_runtime(old_model), patch.object(thesis_correction, "valid_belief_updates", old_check), \
                patch("finauditgate.adapters.model_http.model_http_client",
                      return_value=SimpleNamespace(close=lambda: None)), self.assertRaises(ApplicationError):
            old_app.handle(command)
        self.assertEqual(16, old_budget.calls)
        execution = next(old_root.glob("application/thesis-executions/*"))
        old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(old_files[Path("runtime-receipt.json")])
        self.assertEqual({"node": "Portfolio Manager", "kind": "ForwardRevision",
                          "reason": "THESIS_DECISION_UPDATE_INVALID"}, runtime["recovery"]["halted"])
        original_revision = json.loads(runtime["model_calls"][15]["output"][0]["content"])
        self.assertEqual(MAINTAIN_RESTATEMENT, original_revision["belief_updates"][1]["new_statement"])
        new_budget = make_budget()
        model = FourAnalystLLM()
        app = FinResearchOps(artifact_root=self.root / "resumed-belief", researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=new_budget, resume_from=execution,
            replay_presentation_failure=True))
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        self.assertEqual(["FinalResearchReport"], [r["schema"] for r in model.requests])
        self.assertEqual(17, new_budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], new_budget.receipt()["usage"][:16])
        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"][:16])
        self.assertEqual(16, view.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(original_revision, view.latest_report["forward_revision"])
        final_payload = model.requests[0]["payload"]
        self.assertEqual(original_revision["belief_updates"], final_payload["research_resolution"]["belief_updates"])
        self.assertEqual(runtime["recovery"]["halted"],
                         view.latest_report["reused_calls"]["presentation_replay"]["original_halt"])
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_complete_captured_final_label_replays_without_any_new_call_but_not_unbound_amounts(self):
        command = ResearchThesis("AURORA", date(2026, 3, 2), "只重放已经返回的终稿，不重抽研究。",
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)

        def make_budget():
            return ModelBudget(ceiling_cny="100", max_calls=24,
                               max_input_bytes=524288, max_output_tokens=65536)

        current_literal = DeliveryContext._literal

        def old_literal(context, text):
            if "FY2024" in text:
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            return current_literal(context, text)

        for has_unbound_amount in (False, True):
            with self.subTest(unbound_amount=has_unbound_amount):
                old_root = self.root / ("amount" if has_unbound_amount else "label")
                old_budget = make_budget()
                old_app = FinResearchOps(artifact_root=old_root, researcher=ThesisResearcher(
                    protocol_version=17, live=True, budget=old_budget))
                with patched_native_runtime(LabeledFinalLLM(with_unbound_amount=has_unbound_amount)), \
                        patch.object(DeliveryContext, "_literal", old_literal), \
                        patch("finauditgate.adapters.model_http.model_http_client",
                              return_value=SimpleNamespace(close=lambda: None)), self.assertRaises(ApplicationError):
                    old_app.handle(command)
                self.assertEqual(17, old_budget.calls)
                execution = next(old_root.glob("application/thesis-executions/*"))
                old_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
                runtime = json.loads(old_files[Path("runtime-receipt.json")])
                self.assertEqual({"node": "Portfolio Manager", "kind": "FinalResearchReport",
                                  "reason": "UNBOUND_RESEARCH_NUMBER"}, runtime["recovery"]["halted"])
                original_final = json.loads(runtime["model_calls"][16]["output"][0]["content"])
                restored_budget = make_budget()
                model = FourAnalystLLM()
                app = FinResearchOps(artifact_root=old_root / "resumed", researcher=ThesisResearcher(
                    protocol_version=17, live=True, budget=restored_budget, resume_from=execution,
                    replay_presentation_failure=True))
                with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                        return_value=SimpleNamespace(close=lambda: None)):
                    if has_unbound_amount:
                        with self.assertRaises(ApplicationError):
                            app.handle(command)
                    else:
                        view = app.handle(command)
                        self.assertEqual(old_budget.receipt(), restored_budget.receipt())
                        self.assertEqual(17, view.latest_report["reused_calls"]["used_calls"])
                        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"])
                        self.assertEqual(original_final["summary"]["text"], view.latest_report["final_report"]["summary"]["text"])
                        replay = view.latest_report["reused_calls"]["presentation_replay"]
                        self.assertIs(False, replay["new_primary_calls_allowed"])
                        self.assertEqual("COMPLETE_CAPTURED_FINAL_REVALIDATED", replay["validation"])
                        self.assertEqual(runtime["recovery"]["halted"], replay["original_halt"])
                        self.assertIsNone(view.latest_report["recovery"]["halted"])
                        self.assertEqual(view, app.read_case(view.case_ref))
                self.assertEqual([], model.requests)
                self.assertEqual(old_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
