"""v18 format failures are proven from the saved answer by the standard library alone."""

from copy import deepcopy
import json
import unittest

from finauditgate.adapters.thesis_format import (
    REPAIRABLE_ENUMS, check_enum_repair, enum_repair_messages, format_failure, normalize_format, schema_errors,
)
from finauditgate.adapters.thesis_recovery import failure_reason
from finauditgate.adapters.thesis_responses import response_candidate
from finauditgate.adapters.thesis_schemas_v18 import SCHEMAS
from test_research_numbers import inputs


def saved(content, *, kind="RevisionBrief", node="Bull Researcher", tool_calls=None, **extra):
    return {"run_id": "run-1", "node": node,
            "messages": [[{"type": "system", "content": "SYNTHETIC fixed instructions"},
                          {"type": "human", "content": '{"node":"%s"}' % node}]],
            "thesis_stage": {"kind": kind, "attempt": 1, "reasoning_effort": "max"},
            "output": [{"id": "response-1", "content": content, "tool_calls": tool_calls or [], "finish_reason": "stop"}],
            **extra}


def revision():
    return {"updates": [{"claim_id": f"B{i}", "status": "maintain", "updated_statement": None, "reason": "SYNTHETIC reason",
                         "evidence_refs": ["S01"], "would_change_mind": "SYNTHETIC trigger"} for i in (1, 2)],
            "counter_responses": [{"claim_id": f"S{i}", "response": "dispute", "reason": "SYNTHETIC counter",
                                   "evidence_refs": ["S01"]} for i in (1, 2)]}


def forward_revision(basis="analyst_assumption"):
    return {"changes": [{"scenario_id": "F1", "field": "revenue", "expected_before": 100.0,
                         "replacement": {"value": 90.0, "basis_type": basis, "reason": "SYNTHETIC lower orders",
                                         "evidence_refs": ["S01"]},
                         "correction_basis": "assumption_update", "reason": "SYNTHETIC change", "evidence_refs": ["S01"]}],
            "claim_assessments": [{"claim_id": "B1", "disposition": "conditional", "reason": "SYNTHETIC condition"}],
            "belief_updates": [{"belief_id": f"D{i}", "status": "maintain", "new_statement": None,
                                "update_basis": "no_new_basis", "reason": "SYNTHETIC", "financial_implication": "SYNTHETIC",
                                "evidence_refs": ["S01"]} for i in (1, 2)],
            "unresolved_issues": []}


class SchemaValidatorTest(unittest.TestCase):
    def test_valid_fixtures_have_no_errors(self):
        self.assertEqual([], schema_errors("RevisionBrief", revision()))
        self.assertEqual([], schema_errors("ForwardRevision", forward_revision()))

    def test_every_supported_keyword_reports_its_path(self):
        value = revision()
        value["updates"][0]["status"] = "keep"
        value["updates"][0]["evidence_refs"] = "S01"
        value["counter_responses"][0]["extra"] = 1
        del value["counter_responses"][0]["reason"]
        self.assertEqual({("enum", ("updates", 0, "status")), ("type", ("updates", 0, "evidence_refs")),
                          ("extra", ("counter_responses", 0, "extra")), ("missing", ("counter_responses", 0, "reason"))},
                         {(e, p) for e, p, _ in schema_errors("RevisionBrief", value)})
        draft = forward_revision()
        draft["belief_updates"] = draft["belief_updates"][:1]
        draft["unresolved_issues"] = ["x"] * 7
        draft["changes"][0]["reason"] = ""
        self.assertEqual({("too_short", ("belief_updates",)), ("too_long", ("unresolved_issues",)),
                          ("string_short", ("changes", 0, "reason"))},
                         {(e, p) for e, p, _ in schema_errors("ForwardRevision", draft)})

    def test_dates_numbers_and_unions_follow_the_frozen_schema(self):
        errors = {(e, p) for e, p, _ in schema_errors("UnderwritingDraft", {"market_price_date": "2025-02-30"})}
        self.assertIn(("date", ("market_price_date",)), errors)
        errors = {(e, p) for e, p, _ in schema_errors("UnderwritingDraft", {"market_price_date": None})}
        self.assertNotIn(("date", ("market_price_date",)), errors)
        draft = forward_revision()
        draft["changes"][0]["expected_before"] = float("nan")
        self.assertIn(("finite", ("changes", 0, "expected_before")),
                      {(e, p) for e, p, _ in schema_errors("ForwardRevision", draft)})
        draft["changes"][0]["expected_before"] = True
        self.assertIn(("type", ("changes", 0, "expected_before")),
                      {(e, p) for e, p, _ in schema_errors("ForwardRevision", draft)})
        draft["changes"][0]["expected_before"] = 100
        self.assertEqual([], schema_errors("ForwardRevision", draft))

    def test_patterns_end_at_the_end_of_text_as_in_pydantic(self):
        draft = deepcopy(inputs()[0])
        draft["limitations"] = ["SYNTHETIC limitation"]
        self.assertEqual([], schema_errors("UnderwritingDraft", draft))
        draft["reporting_currency"] = "USD\n"
        draft["scenarios"][0]["scenario_id"] = "F1\n"
        self.assertEqual({("pattern", ("reporting_currency",)), ("pattern", ("scenarios", 0, "scenario_id"))},
                         {(e, p) for e, p, _ in schema_errors("UnderwritingDraft", draft)})
        draft["scenarios"][0]["revenue"]["basis_type"] = "accounting_correction"
        call = saved(json.dumps(draft), kind="UnderwritingDraft", node="Portfolio Manager")
        self.assertEqual((None, []), format_failure(call, "UnderwritingDraft"))

    def test_no_stage_schema_defines_a_note_field(self):
        names = set()

        def walk(node):
            if isinstance(node, dict):
                names.update(node.get("properties", {}))
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(SCHEMAS)
        self.assertFalse({n for n in names if n.endswith("_note") or n.startswith("_")})
        self.assertTrue(REPAIRABLE_ENUMS <= names)


class NormalizeFormatTest(unittest.TestCase):
    def test_null_and_echo_notes_are_dropped_without_touching_other_text(self):
        raw = {"beliefs": [{"belief_id": "D1", "belief_id_note": None},
                           {"belief_id": "D2", "belief_id_note": "D2", "statement": "S", "statement_note": "S"}],
               "decision": {"rating": "REVIEW", "anything_note": None}}
        before = deepcopy(raw)
        self.assertEqual({"beliefs": [{"belief_id": "D1"}, {"belief_id": "D2", "statement": "S"}],
                          "decision": {"rating": "REVIEW"}}, normalize_format(raw, "IndependentAssessment"))
        self.assertEqual(before, raw)

    def test_notes_with_other_content_stay_for_the_schema_to_refuse(self):
        for row in ({"belief_id": "D2", "belief_id_note": "应为D3"}, {"belief_id": "D2", "belief_id_note": ["D2"]},
                    {"value": 1, "value_note": True}, {"orphan_note": "text"}, {"_note": None},
                    {"belief_id": "D2", "belief_id_note": "D2 "}):
            with self.subTest(row=row):
                self.assertEqual(row, normalize_format(row, "IndependentAssessment"))

    def test_protocol_18_candidates_drop_the_observed_echo(self):
        value = forward_revision()
        value["belief_updates"][1]["belief_id_note"] = "D2"
        candidate = response_candidate([{"content": json.dumps(value), "tool_calls": []}], "ForwardRevision", protocol_version=18)
        self.assertEqual(forward_revision(), candidate)
        self.assertIn("belief_id_note", response_candidate([{"content": json.dumps(value), "tool_calls": []}],
                                                          "ForwardRevision", protocol_version=17)["belief_updates"][1])


class FormatFailureTest(unittest.TestCase):
    def test_unparseable_answers_are_proven_from_the_saved_text(self):
        text = json.dumps(revision(), ensure_ascii=False)
        broken = {
            "unescaped_quote": text.replace("SYNTHETIC reason", 'SYNTHETIC "quoted" reason'),
            "early_closer": text.replace('"S01"], "would', '"S01"]}}, "would'),
            "open_string": text[:text.index("SYNTHETIC counter") + 9],
            "prose_only": "SYNTHETIC prose without JSON",
        }
        for name, content in broken.items():
            with self.subTest(name=name):
                self.assertEqual(("UNPARSEABLE", []), format_failure(saved(content), "RevisionBrief"))
        manager = saved("### 评级\n**REVIEW**\n", kind="ResearchEvaluation", node="Research Manager")
        self.assertEqual(("UNPARSEABLE", []), format_failure(manager, "ResearchEvaluation"))
        conflict = saved("", tool_calls=[{"name": "RevisionBrief", "args": {"updates": []}},
                                         {"name": "RevisionBrief", "args": {"updates": []}}])
        self.assertEqual(("UNPARSEABLE", []), format_failure(conflict, "RevisionBrief"))

    def test_readable_answers_with_invalid_content_are_not_unparseable(self):
        layout = ("### 论点处置\n| 论点 | 处置 | 实质理由 |\n|---|---|---|\n| **B1** 经营 | **use** | 合成理由 |\n"
                  "### 评级\n**Buy**\n### 理由\n合成\n### 关键失效条件（研究计划触发项）\n合成\n### 估值依据与缺口\n合成\n")
        self.assertEqual("Buy", response_candidate([{"content": layout, "tool_calls": []}], "ResearchEvaluation",
                                                   protocol_version=18)["plan"]["recommendation"])
        for text in (layout.replace("**Buy**", "**Strong Buy**"), layout.replace("**use**", "**accept**")):
            self.assertEqual((None, []), format_failure(saved(text, kind="ResearchEvaluation", node="Research Manager"),
                                                        "ResearchEvaluation"))
        plan = {"recommendation": "Hold", "rationale": "SYNTHETIC", "strategic_actions": "SYNTHETIC",
                "valuation_basis_and_gaps": "SYNTHETIC version A"}
        competing = {"plan": plan, "assessments": [], "valuation_basis_and_gaps": "SYNTHETIC version B"}
        self.assertEqual((None, []), format_failure(saved(json.dumps(competing), kind="ResearchEvaluation"), "ResearchEvaluation"))
        two = saved(json.dumps(revision()))
        two["output"].append(dict(two["output"][0], id="response-2"))
        self.assertEqual((None, []), format_failure(two, "RevisionBrief"))

    def test_missing_closers_alone_are_not_a_failure(self):
        text = json.dumps(revision())
        self.assertEqual((None, []), format_failure(saved(text[:-1]), "RevisionBrief"))

    def test_missing_reason_uses_the_standard_library_errors(self):
        value = revision()
        del value["updates"][0]["reason"]
        self.assertEqual(("MISSING_REASON", [["updates", 0, "reason"]]), format_failure(saved(json.dumps(value)), "RevisionBrief"))
        value["updates"][0]["extra"] = "x"
        self.assertEqual((None, []), format_failure(saved(json.dumps(value)), "RevisionBrief"))

    def test_only_provenance_labels_outside_their_vocabulary_are_repairable(self):
        value = forward_revision("accounting_correction")
        call = saved(json.dumps(value), kind="ForwardRevision", node="Portfolio Manager")
        self.assertEqual(("ENUM_INVALID", [["changes", 0, "replacement", "basis_type"]]), format_failure(call, "ForwardRevision"))
        refused = []
        for mutate in (lambda v: v["claim_assessments"][0].update(disposition="accept"),
                       lambda v: v["belief_updates"][0].update(status="keep"),
                       lambda v: v["changes"][0].update(scenario_id="F4"),
                       lambda v: v["changes"][0].update(field="price"),
                       lambda v: v["changes"][0]["replacement"].update(basis_type=None),
                       lambda v: v["changes"][0]["replacement"].update(extra="x")):
            value = forward_revision("accounting_correction")
            mutate(value)
            refused.append(format_failure(saved(json.dumps(value), kind="ForwardRevision"), "ForwardRevision"))
        self.assertEqual([(None, [])] * 6, refused)
        report = {"rating": "Strong Buy"}
        self.assertEqual((None, []), format_failure(saved(json.dumps(report), kind="FinalResearchReport"), "FinalResearchReport"))

    def test_valid_content_and_transport_failures_are_not_format_failures(self):
        self.assertEqual((None, []), format_failure(saved(json.dumps(revision())), "RevisionBrief"))
        self.assertEqual((None, []), format_failure(saved(json.dumps([revision()])), "RevisionBrief"))
        self.assertEqual((None, []), format_failure({**saved("{"), "error_type": "APIError"}, "RevisionBrief"))
        self.assertEqual((None, []), format_failure(saved("{"), "DataReview"))

    def test_length_transport_and_empty_are_decided_before_format(self):
        truncated = saved('{"updates": [')
        truncated["output"][0]["finish_reason"] = "length"
        self.assertEqual(("LENGTH", []), failure_reason(truncated, kind="RevisionBrief", protocol_version=18))
        self.assertEqual(("EMPTY_RESPONSE", []), failure_reason(saved("  "), kind="RevisionBrief", protocol_version=18))
        timeout = {**saved(""), "output": [], "error_type": "APITimeoutError"}
        self.assertEqual(("READ_TIMEOUT", []), failure_reason(timeout, kind="RevisionBrief", protocol_version=18))
        self.assertEqual(("UNPARSEABLE", []), failure_reason(saved("{x"), kind="RevisionBrief", protocol_version=18))
        self.assertEqual((None, []), failure_reason(saved("{x"), kind="RevisionBrief", protocol_version=17))


class EnumRepairTest(unittest.TestCase):
    path = [["changes", 0, "replacement", "basis_type"]]

    def test_prompt_lists_the_path_value_vocabulary_and_previous_answer(self):
        before = forward_revision("accounting_correction")
        base = [{"role": "system", "content": "SYNTHETIC"}, {"role": "user", "content": "{}"}]
        prompt = enum_repair_messages(base, "ForwardRevision", before, self.path)
        self.assertEqual(base[1], prompt[1])
        self.assertEqual("SYNTHETIC", base[0]["content"])
        payload = json.loads(prompt[0]["content"].split("\n")[-1])
        self.assertEqual([{"path": self.path[0], "value": "accounting_correction",
                           "allowed": ["reported", "company_guidance", "reference_comparison", "analyst_assumption"]}],
                         payload["enum_paths"])
        self.assertEqual(before, payload["previous_response"])
        self.assertEqual(prompt, enum_repair_messages(base, "ForwardRevision", before, self.path))

    def test_only_the_listed_label_may_change_to_a_listed_value(self):
        before = forward_revision("accounting_correction")
        check_enum_repair(before, forward_revision("reported"), self.path, "ForwardRevision")
        changed = forward_revision("reported")
        changed["changes"][0]["replacement"]["value"] = 95.0
        cases = {"THESIS_REPAIR_CHANGED_EXISTING_CONTENT": changed,
                 "THESIS_REPAIR_ENUM_INVALID": forward_revision("accounting_correction"),
                 "THESIS_REPAIR_ENUM_REQUIRED": forward_revision(None)}
        for code, after in cases.items():
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                check_enum_repair(before, after, self.path, "ForwardRevision")
        with self.assertRaisesRegex(ValueError, "THESIS_REPAIR_PATHS_INVALID"):
            check_enum_repair(before, forward_revision("reported"), [["changes", 0, "correction_basis"]], "ForwardRevision")
        with self.assertRaisesRegex(ValueError, "THESIS_REPAIR_PATHS_INVALID"):
            check_enum_repair(forward_revision(), forward_revision("reported"), self.path, "ForwardRevision")


if __name__ == "__main__":
    unittest.main()
