from dataclasses import fields, replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from finauditgate import FinAuditGate, FrozenDocumentPackage
from finauditgate.application import FinResearchOps, ApplicationError, ReplayRun
from finauditgate.cashflow import CashflowTask, InterimCashflowTask
from finauditgate.research import FundamentalEvidenceTask, ResearchSecurity
from finauditgate.adapters.research_contract import model_evidence_view
from test_research_workflow import ScriptedResearcher

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic/interim_cashflow.html"


class InterimResearchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        (self.workspace / "finaudit-gate/.git").mkdir(parents=True)
        (self.workspace / "private").mkdir()
        self.root = self.workspace / "private/runs"
        self.task = InterimCashflowTask("synthetic-interim", FrozenDocumentPackage(
            "Aurora", "interim.htm", FIXTURE.read_bytes(), date(2026, 8, 20)),
            "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000002/interim.htm",
            "0001111111-26-000002", "1111111", date(2026, 6, 30), date(2025, 6, 30), date(2026, 9, 7))

    def record(self, task=None):
        gate = FinAuditGate(artifact_root=self.root / "core")
        outcome = gate.run(FundamentalEvidenceTask(task or self.task))
        self.assertTrue(gate.replay(outcome.run_ref).consistent)
        return outcome.report

    def altered(self, old, new):
        return replace(self.task, document=replace(self.task.document,
                       document_bytes=self.task.document.document_bytes.replace(old, new)))

    def with_income(self):
        pairs = (("Operating profit", 120, 150), ("Investment income/(loss), net", 5, "(10)"),
                 ("Interest income, net", 5, 5), ("Exchange gains/(losses), net", "(3)", "(9)"),
                 ("Other, net", "(15)", "(6)"), ("Income before tax", 112, 130),
                 ("Income tax", "(22)", "(30)"), ("Net income", 90, 100))
        statement = ('<h2>UNAUDITED CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS</h2>'
            '<p>(in thousands except per share data)</p><table><tr><th></th><th colspan="2">Six Months Ended June 30,</th></tr>'
            '<tr><th></th><th>2025</th><th>2026</th></tr><tr><th></th><th>RMB</th><th>RMB</th></tr>'
            + ''.join(f'<tr><td>{label}</td><td>{a}</td><td>{b}</td></tr>' for label,a,b in pairs) + '</table>')
        return self.altered(b'</body>', statement.encode()+b'</body>')

    def test_profit_change_is_decomposed_with_signed_income_statement_values(self):
        record = self.record(self.with_income())
        bridge = record['analysis']['profit_bridge']
        components = {x['key']: x for x in bridge['components']}
        self.assertEqual('30000.00', components['operating_profit']['change'])
        self.assertEqual('-15000.00', components['investment']['change'])
        self.assertEqual('-8000.00', components['tax']['change'])
        self.assertEqual('10000.00', bridge['net_income']['change'])
        self.assertEqual({'current':'0','comparison':'0'}, bridge['residuals'])
        self.assertIn('profit_bridge', model_evidence_view(record)['analysis'])

    def test_inconsistent_profit_bridge_blocks_model_input(self):
        task = self.with_income()
        bad = replace(task, document=replace(task.document, document_bytes=task.document.document_bytes.replace(
            b'<td>120</td><td>150</td>', b'<td>120</td><td>190</td>')))
        result = self.record(bad)
        self.assertFalse(result['analysis']['metrics'])
        self.assertIn('PROFIT_BRIDGE_UNRECONCILED', result['analysis']['issues'])

    def test_owner_earnings_reconcile_signed_noncontrolling_and_accretion(self):
        task = self.with_income()
        rows = ('<tr><td>Accretion of redeemable noncontrolling interests</td><td>(1)</td><td>(2)</td></tr>'
                '<tr><td>Net income attributable to noncontrolling interests</td><td>(3)</td><td>(4)</td></tr>'
                '<tr><td>Net income attributable to the Company’s shareholders</td><td>86</td><td>94</td></tr>')
        source = task.document.document_bytes.replace(b'</table></body>', rows.encode()+b'</table></body>')
        task = replace(task,document=replace(task.document,document_bytes=source))
        gate = FinAuditGate(artifact_root=self.root/'core')
        outcome = gate.run(FundamentalEvidenceTask(task,include_owner_earnings=True))
        self.assertTrue(gate.replay(outcome.run_ref).consistent)
        record = outcome.report
        self.assertEqual('finauditgate.research-evidence/v6',record['schema_version'])
        self.assertEqual('100000',record['analysis']['metrics']['profit']['current'])
        attribution = record['analysis']['earnings_attribution']
        self.assertEqual('94000',attribution['parent']['current'])
        self.assertEqual({'current':'0','comparison':'0'},attribution['residuals'])
        self.assertEqual(attribution,model_evidence_view(record)['analysis']['earnings_attribution'])
        for broken, code in ((source.replace(b'<td>94</td>',b'<td>96</td>'),None),
                             (source.replace(b'<td>94</td>',b'<td>99</td>'),'OWNER_EARNINGS_UNRECONCILED'),
                             (task.document.document_bytes.replace('Net income attributable to the Company’s shareholders'.encode(),b'Unknown'), 'OWNER_EARNINGS_ROW_REQUIRED')):
            # Rounded presentation may differ within combined rounding bounds.
            bad = replace(task,document=replace(task.document,document_bytes=broken))
            checked = gate.run(FundamentalEvidenceTask(bad,include_owner_earnings=True)).report
            if code:
                self.assertFalse(checked['analysis']['metrics'])
                self.assertIn(code,checked['analysis']['issues'])
            else:
                self.assertTrue(checked['analysis']['metrics'])

    def test_explanation_addresses_each_old_claim_and_does_not_replace_decision(self):
        app = FinResearchOps(artifact_root=self.root, researcher=ScriptedResearcher())
        command = ResearchSecurity(self.with_income(), 'AURORA', 'Cash conversion?')
        old = app.handle(command)
        new = app.handle(replace(command, previous_case_ref=old.case_ref))
        self.assertEqual('finresearchops.investment-research-case/v7', new.latest_report['schema_version'])
        update = new.latest_report['update_explanation']
        self.assertEqual(4, update['budget']['calls'])
        self.assertEqual(new.latest_report['result']['decision'], new.latest_report['result']['calls'][-1]['proposal'])
        self.assertEqual(len(old.latest_report['result']['decision']['claims']), len(update['proposal']['items']))
        self.assertIn('旧观点为什么维持或改变', Path(new.workpaper_paths[0]).read_text())
        self.assertIn('净利润为何变化', Path(new.workpaper_paths[0]).read_text())

    def test_missing_old_claim_and_invented_current_reference_are_rejected(self):
        class BadUpdate(ScriptedResearcher):
            def explain_update(self, payload):
                receipt = super().explain_update(payload)
                receipt['proposal']['items'][0]['current_claim_ids'] = ['current-999']
                return receipt
        command = ResearchSecurity(self.task, 'AURORA', 'Cash conversion?')
        prior = FinResearchOps(artifact_root=self.root, researcher=ScriptedResearcher()).handle(command)
        with self.assertRaisesRegex(ApplicationError, 'UPDATE_REFERENCE_INVALID'):
            FinResearchOps(artifact_root=self.root, researcher=BadUpdate()).handle(replace(command,previous_case_ref=prior.case_ref))

    def test_selects_half_year_currency_and_sign_not_adjacent_quarter(self):
        record = self.record()
        self.assertEqual("100000", record["analysis"]["metrics"]["profit"]["current"])
        contract = next(d for d in record["analysis"]["drivers"] if d["label"] == "Contract liabilities")
        self.assertEqual("-10000", contract["current"])
        self.assertEqual("0", record["analysis"]["metrics"]["unexplained_residual"]["current"])
        self.assertTrue(all(f["source_headers"] and f["scale_sources"] for f in record["analysis"]["facts"]))

    def test_balances_cash_adjustments_and_cash_tax_are_distinct(self):
        record = self.record()
        sup = record["analysis"]["supplemental"]
        self.assertEqual("-8000.00", sup["contract_liability_balance"]["change"])
        self.assertEqual("25000", sup["cash_income_taxes_paid"]["current"])
        self.assertEqual("30000", sup["total_tax_expense"]["current"])
        view = model_evidence_view(record)
        self.assertIn("Matched half-year", view["use_limits"]["cash_tax_comparison"])
        self.assertTrue(all("xbrl_signed_value" not in f for f in view["analysis"]["facts"]))
        balance = next(f for f in view["analysis"]["facts"] if f["row_kind"] == "INSTANT_LIABILITY_BALANCE")
        self.assertEqual("", balance["period_start"])

    def test_blank_or_dash_is_not_zero(self):
        for value in (b"", b"-", b"\xe2\x80\x94"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "MISSING_OR_UNSUPPORTED_NUMBER"):
                self.record(self.altered(b"<td>(10)</td>", b"<td>" + value + b"</td>"))

    def test_missing_duration_currency_scale_and_duplicate_columns_fail(self):
        for old, new, error in ((b"Six Months Ended", b"Unknown Period", "DURATION_HEADER"),
                               (b"<th>2025</th><th>2026</th><th>2026</th>", b"<th>2026</th><th>2026</th><th>2026</th>", "COMPARABLE_COLUMN"),
                               (b"(in thousands)", b"(in millions)", "SCALE_HEADER"),
                               (b"RMB", b"EUR", "COMPARABLE_COLUMN")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.record(self.altered(old, new))

    def test_unreconciled_table_cannot_trigger_a_model_call(self):
        fake = ScriptedResearcher()
        task = self.altered(b"<td>100</td>", b"<td>180</td>")
        with self.assertRaisesRegex(ApplicationError, "CORE_INPUT_UNAVAILABLE"):
            FinResearchOps(artifact_root=self.root, researcher=fake).handle(ResearchSecurity(task, "AURORA", "Cash conversion?"))
        self.assertEqual(0, fake.invocations)

    def test_annual_path_does_not_fall_back_to_plain_interim_table(self):
        annual = CashflowTask(**{f.name: getattr(self.task, f.name) for f in fields(self.task)})
        self.assertFalse(self.record(annual)["analysis"]["metrics"])
        with self.assertRaisesRegex(ValueError, "JANUARY_JUNE"):
            replace(self.task, current_end=date(2026, 12, 31), cutoff=date(2027, 1, 1))

    def test_existing_annual_projection_does_not_gain_interim_fields(self):
        data = (FIXTURE.parent / "cashflow_investigation.html").read_bytes()
        annual = CashflowTask("annual", replace(self.task.document, document_bytes=data),
            self.task.source_url, self.task.accession, self.task.entity_identifier,
            date(2025, 12, 31), date(2024, 12, 31), date(2026, 9, 7), "USD")
        record = self.record(annual)
        view = model_evidence_view(record)
        self.assertNotIn("period_basis", view["source"])
        self.assertTrue(all("period_basis" not in h for h in view["analysis"]["issuer_overview"]))

    def test_tax_component_sum_must_match_reported_total(self):
        record = self.record(self.altered(b"<td>22</td><td>30</td>", b"<td>22</td><td>40</td>"))
        self.assertFalse(record["analysis"]["metrics"])
        self.assertIn("INTERIM_TAX_TOTAL_MISMATCH", record["analysis"]["issues"])

    def test_future_document_is_rejected_before_a_model_call(self):
        with self.assertRaisesRegex(ValueError, "POST_CUTOFF"):
            self.record(replace(self.task, cutoff=date(2026, 7, 1)))

    def test_case_reopens_and_same_source_rerun_is_not_a_new_source(self):
        app = FinResearchOps(artifact_root=self.root, researcher=ScriptedResearcher())
        command = ResearchSecurity(self.task, "AURORA", "Cash conversion?")
        first = app.handle(command)
        second = app.handle(replace(command, previous_case_ref=first.case_ref))
        self.assertEqual("SAME_SOURCE_RERUN", second.latest_report["comparison"]["source_update"]["kind"])
        self.assertEqual(second, FinResearchOps(artifact_root=self.root).read_case(second.case_ref))
        html = Path(second.workpaper_paths[0]).read_text()
        self.assertIn("半年", html)
        self.assertIn("半年实付所得税", html)
        self.assertIn("不算新资料更新", html)
        for call in second.latest_report["result"]["calls"]:
            self.assertNotIn("source_update", json.dumps(call["request"]))

    def test_quarter_note_not_labelled_as_half_year_fact(self):
        record = self.record()
        driver = next(d for d in record["analysis"]["drivers"] if d["label"] == "Contract liabilities")
        final = FinAuditGate(artifact_root=self.root / "core").run(FundamentalEvidenceTask(self.task, (driver["driver_id"],))).report
        for hit in final["steps"][0]["hits"]:
            if "second quarter" in hit["text"]:
                self.assertNotIn(hit["evidence_role"], ("PERIOD_FACT", "PERIOD_CHANGE_EXPLANATION"))

    def test_annual_to_interim_update_preserves_old_thesis_isolation(self):
        annual_data = (FIXTURE.parent / "cashflow_investigation.html").read_bytes()
        annual = CashflowTask("annual", replace(self.task.document, document_bytes=annual_data,
            declared_published_at=date(2026, 3, 1)), self.task.source_url, self.task.accession,
            self.task.entity_identifier, date(2025, 12, 31), date(2024, 12, 31), date(2026, 9, 5), "USD")
        app = FinResearchOps(artifact_root=self.root, researcher=ScriptedResearcher())
        previous = app.handle(ResearchSecurity(annual, "AURORA", "Cash conversion?"))
        original = app.read_case
        reads = []
        def read(ref):
            if ref == previous.case_ref:
                reads.append(len(list((self.root / "application/research-executions").glob("*/new-decision.json"))))
            return original(ref)
        app.read_case = read
        updated = app.handle(ResearchSecurity(self.task, "AURORA", "Cash conversion?", previous_case_ref=previous.case_ref))
        update = updated.latest_report["comparison"]["source_update"]
        self.assertEqual("NEW_SOURCE", update["kind"])
        self.assertEqual("ALREADY_PUBLIC_AT_PREVIOUS_CUTOFF", update["availability_relation"])
        self.assertEqual("全年同比", update["previous"]["period_basis"])
        self.assertEqual("半年同比", update["current"]["period_basis"])
        self.assertTrue(reads and min(reads) >= 2)
        self.assertIn("不是上次研究日之后新公告", Path(updated.workpaper_paths[0]).read_text())


if __name__ == "__main__":
    unittest.main()
