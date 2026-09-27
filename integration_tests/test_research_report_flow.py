"""Formal research report through the real graph, reader, offline command and CLI."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

# Import modules, not TestCase classes, so discovery does not rerun their tests.
import test_four_analyst_flow as four
import test_thesis_delivery as delivery
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application import research_report
from finauditgate.research import RenderResearchReport
import finauditgate.cli as cli


class PendingNumeralLLM(four.FourAnalystLLM):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if not schema or schema.__name__ != "FinalResearchReport":
            return result
        value = json.loads(result.generations[0].message.content)
        value["summary"]["text"] += " DDR5需求仍需观察。"
        return self._result(json.dumps(value, ensure_ascii=False))


class ResearchReportFlowTest(unittest.TestCase):
    setUp = four.FourAnalystFlowTest.setUp
    run_v17 = four.FourAnalystFlowTest.run_case
    run_v16 = delivery.ThesisDeliveryTest.run_case

    def test_new_four_analyst_case_saves_a_verified_formal_report_as_delivery(self):
        view, app, model = self.run_v17()
        formal = Path(view.research_report_path)
        self.assertEqual(Path(view.report_path).with_name("research-report.md"), formal)
        self.assertEqual(str(formal), view.delivery_report_path)
        markdown, workpaper = formal.read_text(), Path(view.report_path).read_text()
        self.assertTrue(formal.with_suffix(".html").read_text().startswith("<!doctype html>"))
        self.assertLess(len(markdown), len(workpaper))
        for marker in four.ANALYSIS_MARKERS.values():
            self.assertNotIn(marker, markdown)  # full stage outputs stay in the workpaper
            self.assertIn(marker, workpaper)
        self.assertNotIn(four.QUANT_NOTE, markdown)  # a cited source, not pasted prose
        self.assertIn("## 投资要点", markdown)
        self.assertIn("### 量化信号的使用", markdown)
        self.assertIn(four.HYPOTHESIS.replace("_", "\\_"), markdown)
        self.assertIn(four.USER_VIEW.replace("_", "\\_"), markdown.split("## 附录一", 1)[1])
        self.assertNotIn(four.USER_VIEW.replace("_", "\\_"), markdown.split("## 附录一", 1)[0])
        self.assertEqual(17, len(model.requests))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_non_value_numeral_saves_a_visible_partial_case_instead_of_halting(self):
        view, app, model = self.run_v17(PendingNumeralLLM())
        self.assertEqual("PARTIAL", view.status)
        self.assertEqual(17, len(model.requests))
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertIn({"field": "summary", "reference": "DDR5", "source_id": None,
                       "reason": "UNBOUND_RESEARCH_NUMBER_PENDING"}, view.latest_report["evidence_check"]["findings"])
        formal = Path(view.research_report_path).read_text()
        self.assertIn("DDR5〔待核〕需求仍需观察", formal)
        pending = len(view.latest_report["evidence_check"]["findings"])
        self.assertIn(f"本报告{pending}个数字未经程序计算或来源绑定，模型评级不能视为获准结论", formal)
        self.assertIn(f"| 未绑定数字 | {pending}项待核，见附录二 |", formal)
        workpaper = Path(view.report_path).read_text()
        self.assertIn("## 待核证据项", workpaper)
        self.assertIn("未绑定数字：未经程序计算或来源绑定", workpaper)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_thirteen_stage_case_also_saves_the_formal_report(self):
        view, app = self.run_v16()
        self.assertEqual("finresearchops.thesis-case/v16", view.latest_report["schema_version"])
        markdown = Path(view.research_report_path).read_text()
        self.assertIn("经营判断保持；历史资料［1］；有效盈利", markdown)
        self.assertIn("Q4实际数据尚未披露", markdown)
        self.assertNotIn("四类分析", markdown)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_changed_or_incomplete_formal_report_is_detected(self):
        view, app, _ = self.run_v17()
        markdown = Path(view.research_report_path)
        page = markdown.with_suffix(".html")
        original, html = markdown.read_bytes(), page.read_bytes()
        markdown.write_bytes(original + b"SYNTHETIC_TAMPER")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)
        markdown.write_bytes(original)
        page.write_bytes(html + b"SYNTHETIC_TAMPER")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)
        page.unlink()
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)
        page.write_bytes(html)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_saved_v1_formal_report_keeps_verifying_in_its_own_format(self):
        original = research_report.render
        with patch.object(research_report, "render", lambda record, version=research_report.V1: original(record, version)):
            view, app, _ = self.run_v17(PendingNumeralLLM())
        markdown = Path(view.research_report_path)
        page = markdown.with_suffix(".html")
        saved = markdown.read_bytes(), page.read_bytes()
        self.assertEqual(research_report.V1, research_report.markdown_version(saved[0]))
        self.assertIn("部分引用或数字未能由程序绑定到原文或计算", saved[0].decode())
        offline = FinResearchOps(artifact_root=self.root)
        self.assertEqual(view.research_report_path, offline.read_case(view.case_ref).research_report_path)
        self.assertEqual(view.research_report_path, offline.handle(RenderResearchReport(view.case_ref)).research_report_path)
        self.assertEqual(saved, (markdown.read_bytes(), page.read_bytes()))  # never rewritten as v2
        markdown.unlink()  # interrupted after the HTML file
        self.assertIsNone(offline.read_case(view.case_ref).research_report_path)
        offline.handle(RenderResearchReport(view.case_ref))
        self.assertEqual(saved, (markdown.read_bytes(), page.read_bytes()))  # finished in the HTML's format
        for forged in (saved[0].replace(research_report.V1.encode(), b"finresearchops.research-report/v9", 1),
                       saved[0].replace(research_report.V1.encode(), research_report.V2.encode(), 1),
                       original(view.latest_report, research_report.V2)[0]):
            with self.subTest(marker=research_report.markdown_version(forged)):
                markdown.write_bytes(forged)
                with self.assertRaises(ApplicationError):
                    offline.read_case(view.case_ref)
        markdown.write_bytes(saved[0])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_saved_case_gains_the_formal_report_offline_without_touching_other_files(self):
        view, _, model = self.run_v17()
        markdown = Path(view.research_report_path)
        page = markdown.with_suffix(".html")
        expected = markdown.read_bytes(), page.read_bytes()
        markdown.unlink()
        page.unlink()  # a Case saved before this format
        offline = FinResearchOps(artifact_root=self.root)  # no researcher can be called
        legacy = offline.read_case(view.case_ref)
        self.assertIsNone(legacy.research_report_path)
        self.assertEqual(legacy.report_path, legacy.delivery_report_path)
        others = {p.name: p.read_bytes() for p in markdown.parent.iterdir()}
        rendered = offline.handle(RenderResearchReport(view.case_ref))
        self.assertEqual(view.research_report_path, rendered.research_report_path)
        self.assertEqual(expected, (markdown.read_bytes(), page.read_bytes()))
        self.assertEqual(others, {p.name: p.read_bytes() for p in markdown.parent.iterdir() if p.name in others})
        self.assertEqual(rendered, offline.handle(RenderResearchReport(view.case_ref)))  # idempotent
        markdown.unlink()  # interrupted after the HTML file
        self.assertIsNone(offline.read_case(view.case_ref).research_report_path)
        self.assertEqual(rendered, offline.handle(RenderResearchReport(view.case_ref)))
        markdown.unlink()
        page.write_bytes(b"SYNTHETIC_CONFLICT")
        with self.assertRaises(ApplicationError):
            offline.handle(RenderResearchReport(view.case_ref))
        self.assertEqual(b"SYNTHETIC_CONFLICT", page.read_bytes())
        self.assertFalse(markdown.exists())
        self.assertEqual(17, len(model.requests))
        with self.assertRaises(ApplicationError):
            offline.handle(RenderResearchReport("case-" + "0" * 64))
        with self.assertRaises(ValueError):
            RenderResearchReport("../case")

    def test_cli_writes_and_reports_the_formal_report(self):
        view, _, _ = self.run_v17()
        markdown = Path(view.research_report_path)
        markdown.unlink()
        markdown.with_suffix(".html").unlink()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["--artifact-root", str(self.root), "render-research-report", "--case-ref", view.case_ref])
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(str(markdown), payload["research_report"])
        self.assertEqual(str(markdown.with_suffix(".html")), payload["research_report_html"])
        self.assertEqual(payload["research_report"], payload["delivery_report"])
        self.assertEqual(view.report_path, payload["workpaper"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.main(["--artifact-root", str(self.root), "inspect-case", "--case-ref", view.case_ref])
        self.assertEqual(str(markdown), json.loads(stdout.getvalue())["research_report"])

    def test_formal_report_reopens_with_the_standard_library_only(self):
        view, _, _ = self.run_v17()
        repo = Path(__file__).parents[1]
        script = (
            "import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
            "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
            "assert Path(v.research_report_path).name=='research-report.md';"
            "assert v.delivery_report_path==v.research_report_path;"
            "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))"
        )
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(self.root), view.case_ref],
                                   env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
