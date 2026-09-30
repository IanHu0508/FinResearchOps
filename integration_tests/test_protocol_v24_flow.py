"""Protocol 24 through the pinned four-analyst graph, with synthetic providers only.

A final report refused only for unbound numbers, whose sentence repair cannot be asked
or does not pass, is delivered with the refused numerals hidden as a PARTIAL Case
instead of stopping the run. These checks establish what is saved and proved, not
whether any answer is right.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_protocol_v18_flow as v18
import test_protocol_v23_flow as v23
from test_protocol_v20_flow import NumberDriftLLM, REPAIRED, VALUE
from finauditgate.adapters.thesis_masking import MARK, SENTENCE_MARK, mask_numbers
from finauditgate.application.research_narrative import change_view
from finauditgate.application.research_report import render as render_formal
from finauditgate.application.thesis_case import validate
from finauditgate.application.thesis_halted import validate as validate_halted
from schema_subset import schema_errors

MASKED = "收入为" + MARK + "元。"
SCHEMAS = Path(__file__).parents[1] / "schemas"
CASE_SCHEMA = json.loads((SCHEMAS / "thesis-case.v24.schema.json").read_text())
HALTED_SCHEMA = json.loads((SCHEMAS / "thesis-halted-case.v1.schema.json").read_text())


class MaskedQualityLLM(NumberDriftLLM, v23.RevisedAftercareLLM):
    """A final report refused beyond repair, then the bounded aftercare with a five-rating revision."""


class WholeSentenceLLM(NumberDriftLLM):
    """Beyond repair, with one more refused sentence naming an unregistered indicator."""
    numeric_sentences: int = 6

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "FinalResearchReport":
            value = json.loads(result.generations[0].message.content)
            value["summary"]["text"] += "价格低于12.5EMA，收入为5亿元。"
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class FullLengthLLM(NumberDriftLLM):
    """Beyond repair, with scenario texts at the answer's length limits that also hold a refused amount."""
    numeric_sentences: int = 6

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "FinalResearchReport":
            value = json.loads(result.generations[0].message.content)
            for key, limit in (("reason", 1600), ("what_changes_the_view", 1200)):
                tail = "利润为3亿元。"
                value["scenario_assessments"][0][key] = "经营条件仍需观察。" * ((limit - len(tail)) // 9) + tail
                value["scenario_assessments"][0][key] = "。" * (limit - len(value["scenario_assessments"][0][key])) + value["scenario_assessments"][0][key]
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class UnmaskableLLM(NumberDriftLLM):
    """The final report is refused for a numeric sentence and also carries a limitation with invalid markup."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "FinalResearchReport":
            value = json.loads(result.generations[0].message.content)
            value["limitations"].append("另见{{bogus:x}}。")
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


def derived_masking(record):
    """The masking a reader derives from the saved final answer of a Case."""
    request = {k: v for k, v in record["request"].items() if k != "user_view"}
    return mask_numbers(record["exchanges"][-1]["parsed"], record["effective_forward_draft"],
                        record["effective_forward_calculations"], record["source_bundle"], request,
                        changes=change_view(record["applied_changes"]),
                        beliefs=record["forward_revision"]["belief_updates"])


class ProtocolV24FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    stdlib_reopen = v23.ProtocolV23FlowTest.stdlib_reopen

    def run_case(self, model=None, *, root=None, protocol=24, resume=None, review=False):
        with patch("finauditgate.adapters.thesis_invocation.sleep"):
            return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def test_a_final_refused_beyond_repair_is_delivered_with_its_numbers_hidden(self):
        view, app, model = self.run_case(NumberDriftLLM(numeric_sentences=6))
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v24", "PARTIAL", "PARTIAL", None, [], 17),
                         (record["schema_version"], record["status"], view.status, record["number_repair"],
                          record["recovery"]["attempts"], len(record["model_calls"])))
        self.assertFalse([r for r in model.requests if r["schema"] == "FinalNumberRepair"])
        rows = record["number_masking"]["sentences"]
        self.assertEqual(6, len(rows))
        self.assertEqual({"field": "summary", "original": VALUE, "masked": MASKED, "mode": "numbers"}, rows[-1])
        self.assertEqual({"summary"}, {r["field"] for r in rows})
        saved, delivered = record["exchanges"][-1]["parsed"]["summary"]["text"], record["final_report"]["summary"]["text"]
        self.assertTrue(saved.endswith(VALUE) and delivered.endswith(MASKED))
        self.assertNotIn("亿元", delivered)
        before, after = deepcopy(record["exchanges"][-1]["parsed"]), deepcopy(record["final_report"])
        before["summary"]["text"] = after["summary"]["text"] = ""
        self.assertEqual(before, after)
        self.assertEqual("COMPLETED", record["evidence_check"]["status"])
        self.assertEqual([], schema_errors(record, CASE_SCHEMA))
        formal = Path(view.research_report_path).read_text()
        self.assertIn("终稿有6个句子含未能由程序核对的数字", formal)
        self.assertIn("| 隐去数值 | 6个句子，原句见过程记录 |", formal)
        self.assertIn(MASKED, formal)
        self.assertNotIn(VALUE, formal)
        self.assertNotIn("整句隐去", formal)
        self.assertIn("终稿有6个句子含未能由程序核对的数字", Path(view.report_path).read_text())
        process = Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("## 终稿数字遮盖", process)
        self.assertIn("字段：summary（隐去被拒数字）", process)
        self.assertEqual(view, app.read_case(view.case_ref))
        self.stdlib_reopen(self.root, view.case_ref, "finresearchops.thesis-case/v24")

    def test_text_no_span_covers_is_hidden_whole_and_the_reports_say_so(self):
        import finauditgate.adapters.thesis_masking as masking
        spots = masking._spots
        # Simulate a refusal no numeral-like span covers; the reader derives under the same rule.
        with patch.object(masking, "_spots", side_effect=lambda text: [] if "EMA" in text else spots(text)):
            view, app, _ = self.run_case(WholeSentenceLLM(), root=self.root / "whole")
            record = view.latest_report
            rows = record["number_masking"]["sentences"]
            self.assertEqual((["numbers"] * 6 + ["sentence"], SENTENCE_MARK + "。"), ([r["mode"] for r in rows], rows[-1]["masked"]))
            self.assertEqual([], schema_errors(record, CASE_SCHEMA))
            formal = Path(view.research_report_path).read_text()
            self.assertIn("终稿有7个句子含未能由程序核对的数字，这些数字已替换为“〔数值待核〕”（其中1句有文字无法逐个隐去数字，已整体隐去）", formal)
            self.assertIn("| 隐去数值 | 7个句子（1句有文字整体隐去），原句见过程记录 |", formal)
            self.assertIn("（其中1句有文字无法逐个隐去数字，已整体隐去）", Path(view.report_path).read_text())
            self.assertIn("字段：summary（文字整体隐去）", Path(view.report_path).with_name("process-record.md").read_text())
            self.assertEqual(view, app.read_case(view.case_ref))
        # Without the simulated refusal the indicator is hidden on its own.
        view, _, _ = self.run_case(WholeSentenceLLM(), root=self.root / "indicator")
        self.assertEqual({"numbers"}, {r["mode"] for r in view.latest_report["number_masking"]["sentences"]})

    def test_masked_text_may_outgrow_the_answer_limits_and_the_schema_accepts_it(self):
        view, app, _ = self.run_case(FullLengthLLM(), root=self.root / "full-length")
        record = view.latest_report
        saved, delivered = record["exchanges"][-1]["parsed"]["scenario_assessments"][0], record["final_report"]["scenario_assessments"][0]
        self.assertEqual((1600, 1200), (len(saved["reason"]), len(saved["what_changes_the_view"])))
        self.assertEqual((1604, 1204), (len(delivered["reason"]), len(delivered["what_changes_the_view"])))
        self.assertEqual([], schema_errors(record, CASE_SCHEMA))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_a_repair_that_fails_or_is_still_refused_ends_in_masking(self):
        for name, model in (("timeout", v23.RepairStopsLLM(failure="timeout")), ("runtime", v23.RepairStopsLLM(failure="runtime")),
                            ("numeric", NumberDriftLLM(repair="numeric")), ("misquoted", NumberDriftLLM(repair="wrong_original"))):
            with self.subTest(name=name):
                view, app, _ = self.run_case(model, root=self.root / name)
                record = view.latest_report
                [attempt] = record["recovery"]["attempts"]
                self.assertEqual(("PARTIAL", "NUMBER_REPAIR", None, {"sentences": [
                                     {"field": "summary", "original": VALUE, "masked": MASKED, "mode": "numbers"}]}),
                                 (record["status"], attempt["reason"], record["number_repair"], record["number_masking"]))
                self.assertIsNotNone(attempt["retry_run_id"])
                self.assertIsNone(record["recovery"]["halted"])
                self.assertEqual([], schema_errors(record, CASE_SCHEMA))
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_a_passing_repair_is_kept_and_nothing_is_hidden(self):
        view, app, _ = self.run_case(NumberDriftLLM())
        record = view.latest_report
        self.assertEqual((None, record["evidence_check"]["status"]), (record["number_masking"], record["status"]))
        self.assertTrue(record["final_report"]["summary"]["text"].endswith(REPAIRED))
        self.assertNotIn("隐去数值", Path(view.research_report_path).read_text())
        self.assertEqual([], schema_errors(record, CASE_SCHEMA))
        self.assertEqual(view, app.read_case(view.case_ref))
        # A repair kept in the report needs the answered call it came from.
        failed = deepcopy(record)
        call = next(c for c in failed["model_calls"] if c["run_id"] == record["number_repair"]["repair_run_id"])
        call["error_type"] = "ReadTimeout"
        with self.assertRaisesRegex(ValueError, "THESIS_NUMBER_REPAIR_INVALID"):
            validate(failed)

    def test_masking_is_proved_from_the_saved_answer(self):
        masked = self.run_case(NumberDriftLLM(numeric_sentences=6), root=self.root / "many")[0].latest_report
        failed = self.run_case(NumberDriftLLM(repair="numeric"), root=self.root / "failed")[0].latest_report
        repaired = self.run_case(NumberDriftLLM(), root=self.root / "repaired")[0].latest_report
        # Masked although the repair passed, or without asking for a repair that could be asked.
        hidden, rows = derived_masking(repaired)
        over_repaired = deepcopy(repaired)
        over_repaired.update(final_report=hidden, number_masking={"sentences": rows}, number_repair=None, status="PARTIAL")
        unasked = deepcopy(over_repaired)
        retry = unasked["recovery"]["attempts"].pop()["retry_run_id"]
        unasked["model_calls"] = [c for c in unasked["model_calls"] if c["run_id"] != retry]

        def mode(r):
            r["number_masking"]["sentences"][0]["mode"] = "sentence"
        for name, record, mutate, reason in (
                ("unhidden", masked, lambda r: r["final_report"]["summary"].update(
                    text=r["exchanges"][-1]["parsed"]["summary"]["text"]), "THESIS_NUMBER_MASKING_INVALID"),
                ("mode", masked, mode, "THESIS_NUMBER_MASKING_INVALID"),
                ("unrecorded", masked, lambda r: r.update(number_masking=None), "THESIS_DELIVERED_ASSESSMENT_CHANGED"),
                ("missing", masked, lambda r: r.pop("number_masking"), "THESIS_NUMBER_REPAIR_INVALID"),
                ("complete", masked, lambda r: r.update(status="COMPLETED"), "THESIS_EVIDENCE_CHECK_CHANGED"),
                ("repair text", failed, lambda r: r.update(number_repair={"repair_run_id": r["recovery"]["attempts"][0]["retry_run_id"],
                    "replacements": [{"field": "summary", "original": VALUE, "replacement": REPAIRED}]}),
                 "THESIS_NUMBER_MASKING_INVALID"),
                ("over-repaired", over_repaired, lambda r: None, "THESIS_NUMBER_MASKING_INVALID"),
                ("unasked", unasked, lambda r: None, "THESIS_NUMBER_MASKING_INVALID")):
            tampered = deepcopy(record)
            mutate(tampered)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, reason):
                validate(tampered)
        v23_case = self.run_case(NumberDriftLLM(), root=self.root / "v23", protocol=23)[0].latest_report
        with self.assertRaisesRegex(ValueError, "THESIS_NUMBER_REPAIR_INVALID"):
            validate({**deepcopy(v23_case), "number_masking": None})

    def test_a_repair_that_was_reserved_but_not_sent_is_masked_from_its_recorded_sentences(self):
        record = self.run_case(v23.RepairStopsLLM(failure="timeout"), root=self.root / "unsent")[0].latest_report
        unsent = deepcopy(record)
        attempt = unsent["recovery"]["attempts"][0]
        unsent["model_calls"] = [c for c in unsent["model_calls"] if c["run_id"] != attempt["retry_run_id"]]
        attempt["retry_run_id"] = None
        validate(deepcopy(unsent))
        self.assertEqual([], schema_errors(unsent, CASE_SCHEMA))
        attempt["number_sentences"] = [{"field": "summary", "sentence": "经营改善。"}]
        with self.assertRaisesRegex(ValueError, "THESIS_NUMBER_MASKING_INVALID"):
            validate(unsent)

    def test_a_repair_stopped_by_an_untrusted_resume_is_not_masked(self):
        with patch("finauditgate.adapters.thesis_correction._repair_numbers",
                   side_effect=ValueError("THESIS_PENDING_RECOVERY_INPUT_MISMATCH")):
            receipt = v18.ProtocolV18FlowTest.halted(self, NumberDriftLLM(), self.root / "untrusted", protocol=24)
        self.assertEqual({"node": "Portfolio Manager", "kind": "FinalResearchReport",
                          "reason": "THESIS_PENDING_RECOVERY_INPUT_MISMATCH"}, receipt["recovery"]["halted"])

    def test_a_final_that_masking_cannot_make_pass_still_stops_and_delivers_what_it_completed(self):
        view, app, _ = self.run_case(UnmaskableLLM(), root=self.root / "unmaskable")
        record = view.latest_report
        self.assertEqual(("HALTED", v23.HALTED, 24, {"node": "Portfolio Manager", "kind": "FinalResearchReport",
                                                     "reason": "UNBOUND_RESEARCH_NUMBER"}, "RULE"),
                         (view.status, record["schema_version"], record["protocol_version"], record["halted"],
                          record["conclusion"]["source"]))
        self.assertEqual([], record["recovery"]["attempts"])
        self.assertIn("| 已完成阶段 | 16/17 |", Path(view.report_path).read_text())
        self.assertEqual([], schema_errors(record, HALTED_SCHEMA))
        self.assertEqual(view, app.read_case(view.case_ref))
        self.stdlib_reopen(self.root / "unmaskable", view.case_ref, v23.HALTED)

    def test_a_stop_after_the_final_report_still_delivers_what_it_completed(self):
        # A failure after the final report was repaired or masked (here the formal layout) stops the run there.
        for name, model in (("masked", NumberDriftLLM(numeric_sentences=6)), ("repaired", NumberDriftLLM())):
            with self.subTest(name=name):
                with patch("finauditgate.application.research_report.render", side_effect=ValueError("SYNTHETIC_LAYOUT_FAILURE")):
                    view, app, _ = self.run_case(model, root=self.root / f"after-{name}")
                record = view.latest_report
                self.assertEqual(("HALTED", {"node": "Portfolio Manager", "kind": "FinalResearchReport",
                                             "reason": "SYNTHETIC_LAYOUT_FAILURE"}),
                                 (view.status, record["halted"]))
                validate_halted(deepcopy(record))
                self.assertEqual([], schema_errors(record, HALTED_SCHEMA))
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_the_aftercare_of_a_masked_case_says_the_case_is_partial(self):
        view, app, _ = self.run_case(MaskedQualityLLM(numeric_sentences=6, quality_mode="conclusion"), root=self.root / "aftercare",
                                     review=True)
        self.assertEqual(("PARTIAL", "COMPLETED"), (view.latest_report["status"], view.review["status"]))
        text = (Path(view.report_path).parent / "quality-report.md").read_text()
        self.assertIn("原终稿有6个句子的未核数字已替换为待核标记，原Case状态为PARTIAL。", text)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_other_stops_are_delivered_as_in_protocol_23(self):
        view, app, _ = self.run_case(v23.GarbageLLM(), root=self.root / "garbage")
        record = view.latest_report
        self.assertEqual(("HALTED", 24, "FinalResearchReport", {"rating": "Buy", "source": "RULE"}),
                         (view.status, record["protocol_version"], record["halted"]["kind"], record["conclusion"]))
        self.assertEqual([], schema_errors(record, HALTED_SCHEMA))
        self.assertEqual(view, app.read_case(view.case_ref))
        formal, _ = render_formal(self.run_case(root=self.root / "clean")[0].latest_report)
        self.assertNotIn("隐去数值", formal.decode())


if __name__ == "__main__":
    unittest.main()
