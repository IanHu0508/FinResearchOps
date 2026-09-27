"""Synthetic regressions for optional review persistence and stdlib reopening."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_thesis_quality_flow as quality_flow
from test_thesis_quality_flow import QualityLLM
from finauditgate.application import thesis_quality
from finauditgate.core.artifacts import canonical_json_bytes


class MultipleUnresolvedLLM(QualityLLM):
    """Keep two bounded unresolved edits so saved ordering is observable."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("QualityReview", "QualityRevision"):
            return result
        value = json.loads(result.generations[0].message.content)
        if kind == "QualityReview":
            finding = value["findings"][0]
            value["findings"] = [
                {**finding, "id": "R1", "original_text": "经营判断保持"},
                {**finding, "id": "R2", "original_text": "Q4实际数据尚未披露"},
            ]
        else:
            edit, resolution = value["edits"][0], value["resolutions"][0]
            value["edits"] = [
                {**edit, "finding_ids": ["R1"], "expected_text": "经营判断保持",
                 "replacement_text": "经营判断仍有待核实"},
                {**edit, "finding_ids": ["R2"], "expected_text": "Q4实际数据尚未披露",
                 "replacement_text": "Q4实际数据尚需核对"},
            ]
            value["resolutions"] = [
                {**resolution, "finding_id": key, "outcome": "unresolved"}
                for key in ("R1", "R2")
            ]
        return self._result(json.dumps(value, ensure_ascii=False))


class QualityPersistenceTest(unittest.TestCase):
    setUp = quality_flow.ThesisQualityFlowTest.setUp
    run_case = quality_flow.ThesisQualityFlowTest.run_case

    def test_multiple_unresolved_findings_reopen_across_hash_seeds(self):
        view, _, _ = self.run_case(MultipleUnresolvedLLM())
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual(["R1", "R2"], view.review["effective"]["unresolved_findings"])
        repo = Path(__file__).parents[1]
        code = (
            "import sys; from pathlib import Path; "
            "from finauditgate.application import FinResearchOps; "
            "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
            "assert v.review['status']=='PARTIAL'; "
            "assert v.review['reason']=='MODEL_REVIEW_NOT_CERTIFICATION'; "
            "assert v.review['effective']['unresolved_findings']==['R1','R2']; "
            "assert Path(v.delivery_report_path).name=='quality-report.md'; "
            "assert v.delivery_rating is None; "
            "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))"
        )
        for seed in ("0", "1", "2", "3", "4", "5"):
            with self.subTest(hash_seed=seed):
                completed = subprocess.run(
                    [str(repo / ".venv/bin/python"), "-S", "-c", code, str(self.root), view.case_ref],
                    env={**os.environ, "PYTHONPATH": str(repo / "src"), "PYTHONHASHSEED": seed},
                    capture_output=True, text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_malformed_optional_trace_preserves_main_case_readability(self):
        view, app, _ = self.run_case()
        directory = Path(view.report_path).parent
        original_case = (directory / "case.json").read_bytes()
        original_report = Path(view.report_path).read_bytes()
        sidecar = json.loads((directory / "review.json").read_bytes())
        for messages in ([], [[]], [[None]], [None], None):
            with self.subTest(messages=messages):
                malformed = deepcopy(sidecar)
                malformed["model_calls"][0]["messages"] = messages
                (directory / "review.json").write_bytes(canonical_json_bytes(malformed))
                reopened = app.read_case(view.case_ref)
                self.assertEqual("PARTIAL", reopened.review["status"])
                self.assertEqual("REVIEW_INTEGRITY_FAILED", reopened.review["reason"])
                self.assertEqual(view.research_report_path, reopened.delivery_report_path)
                self.assertIsNone(reopened.delivery_rating)
                self.assertEqual(view.latest_report, reopened.latest_report)
                self.assertEqual(original_case, (directory / "case.json").read_bytes())
                self.assertEqual(original_report, Path(reopened.report_path).read_bytes())

    def test_quality_rendering_is_repeatable_without_mutating_saved_inputs(self):
        view, app, _ = self.run_case(QualityLLM(quality_mode="financial"))
        self.assertEqual("COMPLETED", view.review["status"])
        record, review = deepcopy(view.latest_report), deepcopy(view.review)
        before_record, before_review = canonical_json_bytes(record), canonical_json_bytes(review)
        first_report = thesis_quality.render_report(record, review)
        first_process = thesis_quality.render_process(record, review)
        self.assertEqual(first_report, thesis_quality.render_report(record, review))
        self.assertEqual(first_process, thesis_quality.render_process(record, review))
        self.assertEqual(before_record, canonical_json_bytes(record))
        self.assertEqual(before_review, canonical_json_bytes(review))
        self.assertEqual(first_report, Path(view.delivery_report_path).read_bytes())
        self.assertEqual(first_process, Path(view.delivery_report_path).with_name("quality-process-record.md").read_bytes())
        self.assertEqual(view, app.read_case(view.case_ref))


if __name__ == "__main__":
    unittest.main()
