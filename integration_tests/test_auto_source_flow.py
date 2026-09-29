"""Acquire only news/social before freezing a real four-analyst execution.

The collector and model are synthetic; the Application, pinned graph, source
freezing, replay and readers are exercised without external network requests.
"""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from native_support import patched_native_runtime
from test_four_analyst_flow import (
    CONSTRAINT, HYPOTHESIS, USER_VIEW, FourAnalystLLM, four_sources,
)
from test_thesis_correction import support
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.research import ResearchThesis
from native_support import payload_of


QUESTION = "合成新闻和社媒自动补充后完成研究，不能以取数缺项推断市场情绪。"
_DEFAULT = object()


def base_sources():
    bundle = four_sources()
    bundle["sources"] = [s for s in bundle["sources"] if s["id"] not in {"NEWS", "SOCIAL"}]
    return bundle


def collected(*, news=True, social=True):
    rows, channels, requests = [], {}, []
    for kind, available in (("news", news), ("social", social)):
        identity = kind.upper()
        channels[kind] = {"status": "COMPLETED" if available else "PARTIAL",
            "count": int(available), "items": [identity] if available else [],
            "gaps": [] if available else ["SYNTHETIC_NO_ELIGIBLE_MATERIAL"],
            "routes": ["SYNTHETIC_ONLY_ROUTE"]}
        requests.append({"channel": kind, "url": "https://synthetic.invalid/" + kind,
            "status": "COMPLETED" if available else "PARTIAL",
            "reason": "SYNTHETIC_CAPTURED" if available else "SYNTHETIC_FETCH_FAILURE_OR_NO_ELIGIBLE_ROWS"})
        if available:
            text = ("SYNTHETIC_AUTO_NEWS: dated event, not an investment recommendation." if kind == "news" else
                    "SYNTHETIC_AUTO_SOCIAL: one dated original post; opinion, not a company fact or representative sample.")
            rows.append({"id": identity, "origin": "SYNTHETIC_" + identity,
                "use": "research", "content": text, "sha256": sha256_hex(text.encode()),
                "availability_note": "SYNTHETIC_ONLY published 2026-03-01; cutoff 2026-03-02; no live data."})
    return {"rows": rows, "channels": channels, "requests": requests,
            "limits": ["SYNTHETIC_ONLY: retrieval coverage is not financial verification."]}


class AcquiredSourceLLM(FourAnalystLLM):
    """An absent channel produces a real unavailable report, not fake evidence."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is None or schema.__name__ != "AnalystReport":
            return result
        payload = payload_of(messages[-1].content)
        required = {"News Analyst": "NEWS", "Sentiment Analyst": "SOCIAL"}.get(payload["node"])
        present = {s["id"] for s in payload["source_bundle"]["sources"]}
        if required is not None and required not in present:
            value = json.loads(result.generations[0].message.content)
            value.update(analysis="SYNTHETIC_UNAVAILABLE：未取得该类合格材料，无法据此判断没有事件或情绪中性。",
                observations=[], coverage="unavailable", evidence_refs=[],
                limits=["SYNTHETIC_ONLY：取数缺口保留，不能编造帖子、新闻或总体情绪。"])
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class AutoSourceFlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    @staticmethod
    def budget():
        return ModelBudget(ceiling_cny="100", max_calls=24,
                           max_input_bytes=524288, max_output_tokens=65536)

    def run_case(self, collector, *, model=None, root=None, sources=_DEFAULT,
                 resume=None, question=QUESTION, as_of=date(2026, 3, 2)):
        root = root or self.root
        model = model or AcquiredSourceLLM()
        budget = self.budget()
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
            protocol_version=17, live=True, budget=budget, resume_from=resume,
            fetch_news_social=True, source_collector=collector))
        command = ResearchThesis("AURORA", as_of, question,
            sources=base_sources() if sources is _DEFAULT else sources,
            review=False, hypotheses=(HYPOTHESIS,), research_constraints=(CONSTRAINT,),
            user_view=USER_VIEW)
        with patched_native_runtime(model), patch(
                "finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_stocktwits_messages",
                side_effect=AssertionError("MODEL_STAGE_SOCIAL_FETCH_FORBIDDEN")), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_reddit_posts",
                side_effect=AssertionError("MODEL_STAGE_SOCIAL_FETCH_FORBIDDEN")):
            view = app.handle(command)
        execution = next(root.glob("application/thesis-executions/*"))
        return view, app, model, budget, execution

    def test_collector_runs_once_before_all_seventeen_model_inputs_and_keeps_base_unchanged(self):
        base = base_sources()
        before = canonical_json_bytes(base)
        fetched = collected()
        collector = Mock(return_value=deepcopy(fetched))
        view, app, model, _, execution = self.run_case(collector, sources=base)
        validate(view.latest_report)
        self.assertEqual(before, canonical_json_bytes(base))
        collector.assert_called_once()
        self.assertEqual(base, collector.call_args.args[0])
        self.assertTrue(Path(collector.call_args.args[1]).resolve().is_relative_to(execution.resolve()))
        record = view.latest_report
        merged = record["source_bundle"]
        self.assertEqual(base["sources"], merged["sources"][:len(base["sources"])])
        self.assertEqual(fetched["rows"], merged["sources"][len(base["sources"]):])
        self.assertEqual(17, len(model.requests))
        self.assertEqual(17, len(record["model_calls"]))
        self.assertEqual(17, len(record["exchanges"]))
        for request in model.requests:
            supplied = {row["id"]: row for row in request["payload"]["source_bundle"]["sources"]}
            for source in fetched["rows"]:
                self.assertIn(source["id"], supplied)
                content = supplied[source["id"]].get("content")
                if content is None:
                    content = "".join(row["text"] for row in supplied[source["id"]]["content_blocks"])
                self.assertEqual(source["content"], content)
            self.assertNotIn(USER_VIEW, request["text"])
        receipt = json.loads((execution / "source-acquisition.json").read_bytes())
        self.assertEqual("FETCHED", receipt["mode"])
        self.assertEqual(sha256_hex(before), receipt["base_bundle_sha256"])
        self.assertEqual(sha256_hex(canonical_json_bytes(merged)), receipt["merged_bundle_sha256"])
        self.assertEqual(["NEWS", "SOCIAL"], receipt["added_source_ids"])
        self.assertEqual({k: v for k, v in fetched.items() if k != "rows"}, receipt["collection"])
        self.assertEqual(base, json.loads((execution / receipt["base_bundle_file"]).read_bytes()))
        self.assertEqual(merged, json.loads((execution / receipt["frozen_bundle_file"]).read_bytes()))
        self.assertEqual(merged, json.loads((execution / "request.json").read_bytes())["sources"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_partial_and_empty_retrieval_preserve_gaps_and_still_complete_all_roles(self):
        for label, news, social in (("news-only", True, False), ("empty", False, False)):
            with self.subTest(label=label):
                fetched = collected(news=news, social=social)
                collector = Mock(return_value=deepcopy(fetched))
                view, app, model, _, execution = self.run_case(collector, root=self.root / label)
                self.assertEqual(17, len(model.requests))
                self.assertEqual("COMPLETED", view.latest_report["status"])
                receipt = json.loads((execution / "source-acquisition.json").read_bytes())
                self.assertEqual(fetched["channels"], receipt["collection"]["channels"])
                self.assertEqual(fetched["requests"], receipt["collection"]["requests"])
                self.assertEqual(fetched["limits"], receipt["collection"]["limits"])
                for node, present in (("News Analyst", news), ("Sentiment Analyst", social)):
                    report = view.latest_report["analyst_reports"][node]
                    self.assertEqual("partial" if present else "unavailable", report["coverage"])
                    if not present:
                        self.assertEqual([], report["observations"])
                        self.assertEqual([], report["evidence_refs"])
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_complete_resume_reuses_frozen_acquisition_and_paid_outputs_for_base_or_merged_input(self):
        base = base_sources()
        first, _, _, original_budget, execution = self.run_case(Mock(return_value=collected()),
            sources=base, root=self.root / "original")
        originals = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        parent_sha = sha256_hex(originals[Path("source-acquisition.json")])
        for label, supplied in (("base", base), ("merged", first.latest_report["source_bundle"])):
            with self.subTest(input=label):
                collector = Mock(side_effect=AssertionError("RESUME_FETCH_FORBIDDEN"))
                view, app, model, budget, replay = self.run_case(collector,
                    sources=deepcopy(supplied), resume=execution, root=self.root / label)
                collector.assert_not_called()
                self.assertEqual([], model.requests)
                self.assertEqual(17, view.latest_report["reused_calls"]["used_calls"])
                self.assertEqual(first.latest_report["model_calls"], view.latest_report["model_calls"])
                self.assertEqual(first.latest_report["source_bundle"], view.latest_report["source_bundle"])
                self.assertEqual(original_budget.receipt(), budget.receipt())
                receipt = json.loads((replay / "source-acquisition.json").read_bytes())
                self.assertEqual("REUSED", receipt["mode"])
                self.assertEqual(str(execution), receipt["parent_execution"])
                self.assertEqual(parent_sha, receipt["parent_receipt_sha256"])
                self.assertEqual(view, app.read_case(view.case_ref))
        self.assertEqual(originals, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})

    def test_changed_base_question_or_date_cannot_trigger_resume_collection_or_model_calls(self):
        _, _, _, _, execution = self.run_case(Mock(return_value=collected()), root=self.root / "original")
        changed_base = base_sources()
        changed_base["sources"][0]["content"] += " SYNTHETIC_CHANGED_BASE"
        changed_base["sources"][0]["sha256"] = sha256_hex(changed_base["sources"][0]["content"].encode())
        changes = ({"sources": changed_base}, {"question": QUESTION + " 改变问题。"},
                   {"as_of": date(2026, 3, 3)})
        for index, options in enumerate(changes):
            with self.subTest(change=options):
                collector = Mock(side_effect=AssertionError("CHANGED_RESUME_FETCH_FORBIDDEN"))
                model = AcquiredSourceLLM()
                with self.assertRaises(ApplicationError):
                    self.run_case(collector, model=model, root=self.root / str(index), resume=execution, **options)
                collector.assert_not_called()
                self.assertEqual([], model.requests)

    def test_missing_base_is_explicitly_rejected_before_fetch_or_model(self):
        collector = Mock(side_effect=AssertionError("NO_BASE_FETCH_FORBIDDEN"))
        model = AcquiredSourceLLM()
        with self.assertRaises(ApplicationError) as caught:
            self.run_case(collector, model=model, sources=None)
        collector.assert_not_called()
        self.assertEqual([], model.requests)
        causes, error = [], caught.exception
        while error is not None:
            causes.append(str(error))
            error = error.__cause__
        self.assertTrue(any("SOURCES" in code or "BASE" in code for code in causes), causes)

    def test_collector_cannot_change_the_base_or_replace_existing_source_ids(self):
        def mutate(base, output_root):
            base["sources"][0]["content"] += " SYNTHETIC_COLLECTOR_MUTATION"
            base["sources"][0]["sha256"] = sha256_hex(base["sources"][0]["content"].encode())
            return collected()

        duplicate = collected()
        duplicate["rows"][0]["id"] = "QUANT"
        for index, effect in enumerate((mutate, lambda *_: duplicate)):
            with self.subTest(index=index):
                base = base_sources()
                before = canonical_json_bytes(base)
                collector = Mock(side_effect=effect)
                model = AcquiredSourceLLM()
                with self.assertRaises(ApplicationError):
                    self.run_case(collector, model=model, sources=base, root=self.root / str(index))
                collector.assert_called_once()
                self.assertEqual(before, canonical_json_bytes(base))
                self.assertEqual([], model.requests)

    def test_auto_acquired_case_reader_stays_standard_library_only(self):
        view, _, _, _, _ = self.run_case(Mock(return_value=collected()))
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
            "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
            "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v17';"
            "assert {'NEWS','SOCIAL'} <= {s['id'] for s in v.latest_report['source_bundle']['sources']};"
            "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        result = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script,
            str(self.root), view.case_ref], env={**os.environ, "PYTHONPATH": str(repo / "src")},
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def stop_after_acquisition(self):
        root = self.root / "collection-only"
        collector = Mock(return_value=collected())
        model = AcquiredSourceLLM()
        with patch("finauditgate.adapters.tradingagents_thesis.CompletedCalls",
                side_effect=ValueError("SYNTHETIC_STOP_AFTER_ACQUISITION")), self.assertRaises(ApplicationError):
            self.run_case(collector, model=model, root=root)
        collector.assert_called_once()
        self.assertEqual([], model.requests)
        execution = next(root.glob("application/thesis-executions/*"))
        self.assertTrue((execution / "source-acquisition.json").is_file())
        self.assertFalse((execution / "runtime-receipt.json").exists())
        self.assertFalse((execution / "model-traces").exists())
        return execution

    def test_acquisition_only_interruption_reuses_sources_then_starts_the_first_model_chain(self):
        execution = self.stop_after_acquisition()
        original = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        collector = Mock(side_effect=AssertionError("ALREADY_ACQUIRED_FETCH_FORBIDDEN"))
        view, app, model, budget, resumed = self.run_case(collector, root=self.root / "after-collection",
            resume=execution)
        collector.assert_not_called()
        self.assertEqual(17, len(model.requests))
        self.assertEqual(17, budget.calls)
        self.assertNotIn("reused_calls", view.latest_report)
        receipt = json.loads((resumed / "source-acquisition.json").read_bytes())
        self.assertEqual("REUSED", receipt["mode"])
        self.assertEqual(0, receipt["network_requests_this_execution"])
        self.assertEqual(sha256_hex(original[Path("source-acquisition.json")]), receipt["parent_receipt_sha256"])
        self.assertEqual(original, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_missing_runtime_with_model_trace_cannot_be_treated_as_acquisition_only(self):
        execution = self.stop_after_acquisition()
        traces = execution / "model-traces"
        traces.mkdir()
        trace = traces / "call-001-request.json"
        trace.write_bytes(canonical_json_bytes({"schema_version": "SYNTHETIC_ONLY",
                                               "notice": "A model request may have been sent."}))
        original = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        collector = Mock(side_effect=AssertionError("AMBIGUOUS_PRIOR_FETCH_FORBIDDEN"))
        model = AcquiredSourceLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(collector, model=model, root=self.root / "ambiguous", resume=execution)
        collector.assert_not_called()
        self.assertEqual([], model.requests)
        self.assertEqual(original, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})

    def test_interrupted_reused_acquisition_cannot_reset_its_parent_model_budget(self):
        first, _, _, original_budget, execution_a = self.run_case(
            Mock(return_value=collected()), root=self.root / "complete-a")
        original_a = {p.relative_to(execution_a): p.read_bytes()
                      for p in execution_a.rglob("*") if p.is_file()}
        collector_b = Mock(side_effect=AssertionError("HANDOFF_MUST_NOT_FETCH"))
        model_b = AcquiredSourceLLM()
        root_b = self.root / "interrupted-b"
        with patch("finauditgate.adapters.tradingagents_thesis.CompletedCalls",
                   side_effect=ValueError("SYNTHETIC_INTERRUPTED_REUSED_HANDOFF")), \
                self.assertRaises(ApplicationError):
            self.run_case(collector_b, model=model_b, root=root_b, resume=execution_a)
        collector_b.assert_not_called()
        self.assertEqual([], model_b.requests)
        execution_b = next(root_b.glob("application/thesis-executions/*"))
        handoff = json.loads((execution_b / "source-acquisition.json").read_bytes())
        self.assertEqual("REUSED", handoff["mode"])
        self.assertEqual(str(execution_a), handoff["parent_execution"])
        self.assertFalse((execution_b / "runtime-receipt.json").exists())
        self.assertFalse((execution_b / "model-traces").exists())
        original_b = {p.relative_to(execution_b): p.read_bytes()
                      for p in execution_b.rglob("*") if p.is_file()}

        collector_c = Mock(side_effect=AssertionError("AMBIGUOUS_HANDOFF_MUST_NOT_FETCH"))
        model_c = AcquiredSourceLLM()
        failure = None
        try:
            self.run_case(collector_c, model=model_c, root=self.root / "reject-c", resume=execution_b)
        except ApplicationError as exc:
            failure = exc
        collector_c.assert_not_called()
        self.assertEqual(0, len(model_c.requests),
                         "A copied REUSED receipt without a runtime restarted the already-paid model chain")
        self.assertIsInstance(failure, ApplicationError)
        self.assertEqual(original_b, {p.relative_to(execution_b): p.read_bytes()
                                      for p in execution_b.rglob("*") if p.is_file()})

        safe_collector = Mock(side_effect=AssertionError("VALID_PARENT_RESUME_MUST_NOT_FETCH"))
        resumed, app, model, budget, _ = self.run_case(safe_collector,
            root=self.root / "resume-a", resume=execution_a)
        safe_collector.assert_not_called()
        self.assertEqual([], model.requests)
        self.assertEqual(original_budget.receipt(), budget.receipt())
        self.assertEqual(first.latest_report["model_calls"], resumed.latest_report["model_calls"])
        self.assertEqual(17, resumed.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(resumed, app.read_case(resumed.case_ref))
        self.assertEqual(original_a, {p.relative_to(execution_a): p.read_bytes()
                                      for p in execution_a.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
