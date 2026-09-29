"""Protocol 23 through the pinned four-analyst graph, with synthetic providers only.

Every final report concludes with a rating and a confidence beside a fixed rule's
reference rating, and a run that stops still delivers what it completed. These
checks establish what is sent, saved and proved, not whether any rating is right.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_protocol_v18_flow as v18
from test_four_analyst_flow import FourAnalystLLM, FourAnalystQualityLLM
from test_protocol_v20_flow import NumberDriftLLM
import httpx
from openai import APITimeoutError
from test_protocol_v22_flow import ContentDriftLLM, cut_off
from finauditgate.adapters.thesis_correction import CONCLUDE_FINAL, concluded_schemas, correction_schemas
from finauditgate.adapters.thesis_protocol import CONCLUDE_PLAN, FORWARD_PE_V23, schemas
from finauditgate.adapters.thesis_recovery import call_messages
from finauditgate.adapters.thesis_schemas_v23 import SCHEMAS_V23
from finauditgate.application import ApplicationError
from finauditgate.application.thesis_case import validate
from finauditgate.application.thesis_halted import validate as validate_halted
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from native_support import payload_of

HALTED = "finresearchops.thesis-halted-case/v1"


class GarbageLLM(FourAnalystLLM):
    """Every answer of one stage is prose, never JSON; KeyboardInterrupt when asked to interrupt."""
    target: str = "FinalResearchReport"
    node: str | None = None
    interrupt: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == self.target and (
                self.node is None or payload_of(messages[-1].content)["node"] == self.node):
            if self.interrupt:
                raise KeyboardInterrupt
            return self._result("这不是JSON")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class KeptUnknownAnalystLLM(FourAnalystLLM):
    """The news analyst answers once, completely but at the output limit, citing a source it was not given."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is None or schema.__name__ != "AnalystReport" or payload_of(messages[-1].content)["node"] != "News Analyst":
            return result
        value = json.loads(result.generations[0].message.content)
        value["evidence_refs"] = [*value["evidence_refs"], "S99_SYNTHETIC_UNKNOWN"]
        raise cut_off(json.dumps(value, ensure_ascii=False))


class RepairStopsLLM(NumberDriftLLM):
    """The final report is refused for one numeric sentence and the sentence repair call then fails."""
    failure: str = "timeout"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "FinalNumberRepair":
            if self.failure == "timeout":
                raise APITimeoutError(request=httpx.Request("POST", "https://synthetic.invalid"))
            raise RuntimeError("synthetic provider failure")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class DuplicateUpdateLLM(FourAnalystLLM):
    """The bull researcher's revision answers one of its claims twice, so its coverage check fails."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is None or schema.__name__ != "RevisionBrief" or payload_of(messages[-1].content)["node"] != "Bull Researcher":
            return result
        value = json.loads(result.generations[0].message.content)
        value["updates"][1]["claim_id"] = value["updates"][0]["claim_id"]
        return self._result(json.dumps(value, ensure_ascii=False))


class RevisedAftercareLLM(FourAnalystQualityLLM):
    """The aftercare revision proposes a given rating, which only the five-rating vocabulary may allow."""
    revised_rating: str = "Underweight"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "QualityRevision":
            value = json.loads(result.generations[0].message.content)
            value["rating"]["value"] = self.revised_rating
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class ProtocolV23FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    halted = v18.ProtocolV18FlowTest.halted

    def run_case(self, model=None, *, root=None, protocol=23, resume=None, review=False):
        with patch("finauditgate.adapters.thesis_invocation.sleep"):
            return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def stdlib_reopen(self, root, case_ref, schema_version):
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  f"assert v.latest_report['schema_version']=={schema_version!r};"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(root), case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_frozen_v23_schemas_equal_the_live_models(self):
        types = schemas()
        types.update(correction_schemas(types, bound=True, selected=True))
        live = concluded_schemas(types)
        self.assertEqual(sorted(SCHEMAS_V23), sorted(live))
        for kind, frozen in SCHEMAS_V23.items():
            with self.subTest(kind=kind):
                self.assertEqual(frozen, live[kind].model_json_schema())

    def test_a_new_run_concludes_beside_the_rule_and_reopens_with_the_standard_library(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v23", "COMPLETED", "Hold", "low"),
                         (record["schema_version"], record["status"], record["final_report"]["rating"],
                          record["final_report"]["confidence"]))
        self.assertEqual(("COMPUTED", "Buy"), (record["rule_rating"]["status"], record["rule_rating"]["rating"]))
        self.assertEqual("Sell", record["research_evaluation"]["plan"]["recommendation"])
        stage = {e["kind"]: e for e in record["exchanges"]}
        final_sent = stage["FinalResearchReport"]["messages"][1]["content"]
        self.assertEqual({"method": record["rule_rating"]["method"], "status": "COMPUTED", "rating": "Buy"},
                         payload_of(final_sent)["rating_rule"])
        self.assertIn(CONCLUDE_FINAL, final_sent)
        self.assertIn(FORWARD_PE_V23, stage["UnderwritingDraft"]["messages"][1]["content"])
        self.assertIn(CONCLUDE_PLAN, stage["ResearchEvaluation"]["messages"][1]["content"])
        formal = Path(view.research_report_path).read_text()
        self.assertIn("| 置信度 | 低 |", formal)
        self.assertIn("| 规则参考评级 | 买入（Buy）：2个情景回报等权平均+48.75% |", formal)
        self.assertIn("## 结论与规则参考评级", Path(view.report_path).read_text())
        self.assertEqual(view, app.read_case(view.case_ref))
        self.stdlib_reopen(self.root, view.case_ref, "finresearchops.thesis-case/v23")
        smuggled = deepcopy(record)
        manager = next(e for e in smuggled["exchanges"] if e["kind"] == "ResearchEvaluation")
        call = next(c for c in smuggled["model_calls"] if any(o.get("id") == manager["response_id"] for o in c["output"]))
        answer = json.loads(call["output"][0]["content"])
        answer["plan"]["recommendation"] = manager["parsed"]["plan"]["recommendation"] = "REVIEW"
        call["output"][0]["content"] = json.dumps(answer, ensure_ascii=False)
        smuggled["research_evaluation"] = deepcopy(manager["parsed"])
        with self.assertRaisesRegex(ValueError, "THESIS_OUTPUT_SCHEMA_INVALID"):
            validate(smuggled)
        for mutate, reason in ((lambda r: r["rule_rating"].update(rating="Sell"), "THESIS_RULE_RATING_MISMATCH"),
                               (lambda r: r["final_report"].update(confidence="high"), "THESIS_DELIVERED_ASSESSMENT_CHANGED")):
            tampered = deepcopy(record)
            mutate(tampered)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                validate(tampered)

    def test_a_stopped_run_delivers_what_it_completed_with_its_conclusion(self):
        for target, node, conclusion, done in (("FinalResearchReport", None, {"rating": "Buy", "source": "RULE"}, 16),
                                               ("UnderwritingDraft", None, {"rating": "Sell", "source": "RESEARCH_MANAGER"}, 14),
                                               ("InitialBrief", "Bull Researcher", {"rating": None, "source": None}, 4)):
            with self.subTest(target=target):
                root = self.root / target
                view, app, _ = self.run_case(GarbageLLM(target=target, node=node), root=root)
                record = view.latest_report
                self.assertEqual(("HALTED", HALTED, target, conclusion, conclusion["rating"]),
                                 (view.status, record["schema_version"], record["halted"]["kind"], record["conclusion"],
                                  record["signal"]))
                self.assertEqual(("DEFERRED", "HALTED_DELIVERY"), (view.review["status"], view.review["reason"]))
                text = Path(view.report_path).read_text()
                self.assertIn(f"| 已完成阶段 | {done}/17 |", text)
                self.assertIn("未形成" if conclusion["rating"] is None else "| 结论来源 |", text)
                self.assertTrue((next(root.glob("application/thesis-executions/*")) / "failure.json").exists())
                self.assertEqual(view, app.read_case(view.case_ref))
                self.stdlib_reopen(root, view.case_ref, HALTED)

    def test_a_stopped_stage_keeps_its_answer_for_review_but_not_as_a_result(self):
        view, _, _ = self.run_case(ContentDriftLLM(drift="forward", actions=["kept"], seen={}), root=self.root / "kept")
        record = view.latest_report
        self.assertEqual({"node": "Portfolio Manager", "kind": "UnderwritingDraft", "reason": "THESIS_FORWARD_INCONSISTENT"},
                         record["halted"])
        self.assertEqual(("UnderwritingDraft", None, None, None),
                         (record["exchanges"][-1]["kind"], record["forward_draft"], record["forward_calculations"],
                          record["rule_rating"]))
        self.assertEqual("RESEARCH_MANAGER", record["conclusion"]["source"])

    def test_a_halted_case_is_proved_like_a_complete_case(self):
        view, app, _ = self.run_case(GarbageLLM(), root=self.root / "proved")
        record = view.latest_report
        validate_halted(deepcopy(record))
        def swap_first_drafts(r):
            # Bear before bull, with their calls swapped too: the call order stays valid, the stage order does not.
            i, j = (next(k for k, e in enumerate(r["exchanges"]) if (e["node"], e["kind"]) == (node, "InitialBrief"))
                    for node in ("Bull Researcher", "Bear Researcher"))
            calls = [next(k for k, c in enumerate(r["model_calls"]) if any(o.get("id") == r["exchanges"][x]["response_id"]
                                                                            for o in c.get("output", []))) for x in (i, j)]
            r["exchanges"][i], r["exchanges"][j] = r["exchanges"][j], r["exchanges"][i]
            r["model_calls"][calls[0]], r["model_calls"][calls[1]] = r["model_calls"][calls[1]], r["model_calls"][calls[0]]

        foreign = deepcopy(record["model_calls"][-1])
        foreign.update(node="Trader", run_id="synthetic-foreign-call")
        for output in foreign["output"]:
            output["id"] = "synthetic-foreign-output"
        for name, mutate in (
                ("conclusion", lambda r: r["conclusion"].update(rating="Sell")),
                ("rule", lambda r: r["rule_rating"].update(expected_return=0.01)),
                ("signal", lambda r: r.update(signal="Hold")),
                ("stage", lambda r: r["risk_briefs"]["Aggressive Analyst"].update(analysis="改写")),
                ("withheld", lambda r: r.update(research_evaluation=None)),
                ("halt", lambda r: r["halted"].update(reason="OTHER")),
                ("tail", lambda r: r["model_calls"].append(deepcopy(foreign))),
                ("reordered", lambda r: r["exchanges"].insert(0, r["exchanges"].pop(4))),
                ("swapped", swap_first_drafts),
                ("extra", lambda r: r.update(final_report={}))):
            tampered = deepcopy(record)
            mutate(tampered)
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                validate_halted(tampered)
        directory = Path(view.report_path).parent
        report = directory / "halted-report.md"
        original = report.read_bytes()
        report.chmod(0o644)
        report.write_bytes(original.replace("已完成阶段".encode(), "全部完成阶段".encode()))
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)
        report.write_bytes(original)
        self.assertEqual(view, app.read_case(view.case_ref))
        forged = json.loads((directory / "case.json").read_bytes())
        forged["conclusion"]["rating"] = "Sell"
        raw = canonical_json_bytes(forged)
        target = directory.parent / ("case-" + sha256_hex(raw))
        target.mkdir()
        (target / "case.json").write_bytes(raw)
        (target / "halted-report.md").write_bytes((directory / "halted-report.md").read_bytes())
        with self.assertRaises(ApplicationError):
            app.read_case(target.name)

    def test_a_stop_in_the_sentence_repair_still_delivers(self):
        for failure, reason in (("timeout", "APITimeoutError"), ("runtime", "RuntimeError")):
            with self.subTest(failure=failure):
                view, app, _ = self.run_case(RepairStopsLLM(failure=failure), root=self.root / f"repair-{failure}")
                record = view.latest_report
                self.assertEqual(("HALTED", {"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": reason},
                                  "RULE", ["NUMBER_REPAIR"]),
                                 (view.status, record["halted"], record["conclusion"]["source"],
                                  [a["reason"] for a in record["recovery"]["attempts"]]))
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_forged_progress_in_a_halted_case_is_refused(self):
        from finauditgate.adapters.thesis_degrade import placeholder
        manager = self.run_case(GarbageLLM(target="ResearchEvaluation"), root=self.root / "manager")[0].latest_report
        self.assertEqual("ResearchEvaluation", manager["halted"]["kind"])
        template = next(c for c in manager["model_calls"] if c["node"] == "Research Manager")
        fake = {**deepcopy(template), "node": "Trader", "run_id": "fabricated-trader-call", "output": [],
                "error_type": "RuntimeError", "thesis_stage": {"kind": "ExecutionReview", "attempt": 1, "reasoning_effort": "low"}}
        fake.pop("failure_response", None)
        later = deepcopy(manager)
        later["model_calls"].insert(0, fake)
        later["recovery"]["degraded"].append({"node": "Trader", "kind": "ExecutionReview", "reason": "RuntimeError",
                                              "run_ids": ["fabricated-trader-call"]})
        later["execution_review"] = placeholder("Trader", "ExecutionReview", "RuntimeError")
        bull = next(c for c in manager["model_calls"] if c["node"] == "Bull Researcher")
        pending = deepcopy(manager)
        pending["model_calls"].insert(0, {**deepcopy(bull), "run_id": "fabricated-bull-call", "output": [],
                                          "error_type": "APITimeoutError"})
        pending["recovery"]["attempts"].append({"node": "Bull Researcher", "kind": "InitialBrief", "reason": "READ_TIMEOUT",
            "missing_reason_paths": [], "enum_paths": [], "schema_errors": [], "number_sentences": [], "check": None,
            "failed_run_id": "fabricated-bull-call", "retry_run_id": None, "retry_effort": "high"})
        relabeled = deepcopy(manager)
        for field in ("halted",):
            relabeled[field]["reason"] = "THESIS_RESUME_INPUT_MISMATCH"
        relabeled["recovery"]["halted"]["reason"] = "THESIS_RESUME_INPUT_MISMATCH"
        for name, forged in (("degraded after the stop", later), ("pending before the stop", pending), ("integrity stop", relabeled)):
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                validate_halted(forged)
        revision = self.run_case(DuplicateUpdateLLM(), root=self.root / "coverage")[0].latest_report
        self.assertEqual(({"node": "Bull Researcher", "kind": "RevisionBrief", "reason": "THESIS_CLAIM_COVERAGE_INVALID"}, {}),
                         (revision["halted"], revision["revisions"]))
        moved = deepcopy(revision)
        moved["halted"] = {**moved["halted"], "node": "Bear Researcher"}
        moved["recovery"]["halted"] = deepcopy(moved["halted"])
        moved["revisions"] = {"Bull Researcher": deepcopy(revision["exchanges"][-1]["parsed"])}
        with self.assertRaisesRegex(ValueError, "THESIS_CLAIM_COVERAGE_INVALID"):
            validate_halted(moved)

    def test_aftercare_keeps_the_five_ratings_and_follows_the_revision(self):
        view, app, _ = self.run_case(RevisedAftercareLLM(quality_mode="conclusion"), root=self.root / "quality", review=True)
        self.assertEqual(("COMPLETED", "Underweight"), (view.review["status"], view.review["effective"]["final_report"]["rating"]))
        text = (Path(view.report_path).parent / "quality-report.md").read_text()
        self.assertIn("模型评级：减持（Underweight）（校订改变了评级，原置信度不适用）", text)
        self.assertEqual(view, app.read_case(view.case_ref))
        # A revised forward parameter moves the rule with it; the Case keeps the rule of its own scenarios.
        view, app, _ = self.run_case(RevisedAftercareLLM(quality_mode="financial", revised_rating="Hold"),
                                     root=self.root / "quality-financial", review=True)
        effective = view.review["effective"]
        self.assertNotEqual(view.latest_report["effective_forward_calculations"], effective["effective_forward_calculations"])
        from finauditgate.application.research_conclusion import rule_text
        from finauditgate.core.rating_rule import rule_rating
        revised = rule_text(rule_rating(effective["effective_forward_calculations"], 12))
        self.assertNotEqual(rule_text(view.latest_report["rule_rating"]), revised)
        text = (Path(view.report_path).parent / "quality-report.md").read_text()
        self.assertIn("规则参考评级：" + revised, text)
        self.assertIn("模型评级：中性（Hold），置信度低", text)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_an_analyst_answer_kept_at_the_output_limit_that_fails_its_checks_is_left_out(self):
        # A complete answer at the limit is kept or refused, never asked again; the reader agrees (protocol 20 on).
        for protocol in (22, 23):
            with self.subTest(protocol=protocol):
                view, app, _ = self.run_case(KeptUnknownAnalystLLM(), protocol=protocol, root=self.root / f"kept-{protocol}")
                record = view.latest_report
                [row] = record["recovery"]["degraded"]
                self.assertEqual(("News Analyst", "THESIS_UNKNOWN_SOURCE_REFERENCE", "PARTIAL", []),
                                 (row["node"], row["reason"], record["status"], record["recovery"]["attempts"]))
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_earlier_protocols_and_interruptions_are_not_delivered(self):
        self.halted(GarbageLLM(), self.root / "v22", protocol=22)
        with self.assertRaises(KeyboardInterrupt):
            self.run_case(GarbageLLM(interrupt=True), root=self.root / "interrupt")
        self.assertFalse(list((self.root / "interrupt").glob("application/thesis-cases/*")))


if __name__ == "__main__":
    unittest.main()
