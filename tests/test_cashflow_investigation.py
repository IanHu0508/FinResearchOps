from dataclasses import replace
from datetime import date
from decimal import Inexact, Rounded, localcontext
import json
from pathlib import Path
import tempfile
import unittest

from finauditgate import FinAuditGate, FrozenDocumentPackage
from finauditgate.cashflow import CashflowTask
from finauditgate.adapters.cashflow_ollama import OllamaCashflowPlanner
from finauditgate.application import FinResearchOps, ReplayRun
from finauditgate.cashflow import InvestigateCashflow


FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic/cashflow_investigation.html"


class CashflowInvestigationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "finaudit-gate").mkdir()
        (self.workspace / "finaudit-gate/.git").mkdir()
        (self.workspace / "private").mkdir()
        self.root = self.workspace / "private/runs"
        self.task = CashflowTask("synthetic-cashflow", FrozenDocumentPackage(
            "aurora", "synthetic.html", FIXTURE.read_bytes(), date(2026, 3, 1)),
            "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000001/synthetic.htm",
            "0001111111-26-000001", "1111111", date(2025, 12, 31), date(2024, 12, 31),
            date(2026, 3, 2), "USD")

    def changed(self, old, new):
        return replace(self.task, document=replace(self.task.document,
            document_bytes=self.task.document.document_bytes.replace(old.encode(), new.encode())))

    def test_missing_comparative_can_use_an_explicit_same_filing_zero(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = re.sub(r'<ix:nonFraction id="ar24".*?</ix:nonFraction>', '—', text)
        support = ('<p>Additional disclosure: <ix:nonFraction id="explicit-zero" '
                   'name="us-gaap:IncreaseDecreaseInAccountsReceivable" contextRef="fy2024" '
                   'unitRef="money" scale="6" decimals="-6" format="ixt:fixed-zero">'
                   '—</ix:nonFraction>.</p>')
        text = text.replace('</body>', support + '</body>')
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.encode()))
        outcome = FinAuditGate(artifact_root=self.root).run(task)
        driver = next(d for d in outcome.report['analysis']['drivers'] if d['label'] == 'Accounts receivable')
        self.assertEqual('0', driver['comparison'])
        refs = [f['source']['anchor'] for f in outcome.report['analysis']['facts'] if f['fact_id'] in driver['fact_ids']]
        self.assertIn('explicit-zero', refs)

    def test_financial_narrative_with_plural_wording_beats_current_year_risk_text(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = re.sub(r'<p id="receivables-note">.*?</p>', '', text)
        text = text.replace('</body>', '<h2>2025 operating performance</h2>'
            '<p>Trade receivables increased because settlement dates were extended for distributors. '
            'Collection of the outstanding balances moved into the next year, reducing cash received in this period.</p>'
            '<p>In 2025 accounts receivable may increase if customers encounter financial difficulties. '
            'Future accounts receivable losses could adversely affect our cash position and our financial condition.</p></body>')
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.encode()))
        outcome = FinAuditGate(artifact_root=self.root).run(task)
        self.assertIn('settlement dates were extended', outcome.report['steps'][0]['hits'][0]['text'])

    def test_cashflow_overview_is_retrieved_before_optional_driver_search(self):
        text = self.task.document.document_bytes.decode().replace('</body>',
            '<h2>Operating activities</h2><p>Net cash provided by operating activities fell in 2025 compared with 2024. '
            'The decrease was primarily due to inventory build-up ahead of the product launch. '
            'Management expects the seasonal balances to unwind after the launch.</p></body>')
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.encode()))
        record = FinAuditGate(artifact_root=self.root).run(task).report
        self.assertIn('inventory build-up ahead of the product launch', json.dumps(record))

    def test_conflicting_counterpart_sources_do_not_fill_missing_value(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = re.sub(r'<ix:nonFraction id="ar24".*?</ix:nonFraction>', '—', text)
        support = '<p>' + ''.join('<ix:nonFraction name="us-gaap:IncreaseDecreaseInAccountsReceivable" '
            'contextRef="fy2024" unitRef="money" scale="6" decimals="-6">' + value + '</ix:nonFraction>'
            for value in ('10','30')) + '</p>'
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.replace('</body>', support+'</body>').encode()))
        a = FinAuditGate(artifact_root=self.root).run(task).report['analysis']
        ar = next(d for d in a['drivers'] if d['label']=='Accounts receivable')
        self.assertIsNone(ar['comparison'])
        self.assertIn('COUNTERPART_FACT_CONFLICT',a['issues'])

    def test_counterpart_nonzero_uses_the_cashflow_sign_relation(self):
        import re
        text = self.task.document.document_bytes.decode()
        tag = re.search(r'<ix:nonFraction id="ar24".*?</ix:nonFraction>',text).group()
        text = text.replace(tag,'—').replace('</body>','<p>Another source disclosure '+tag+'</p></body>')
        task = replace(self.task,document=replace(self.task.document,document_bytes=text.encode()))
        outcome = FinAuditGate(artifact_root=self.root).run(task)
        ar = next(d for d in outcome.report['analysis']['drivers'] if d['label']=='Accounts receivable')
        self.assertEqual('10000000',ar['comparison'])
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(outcome.run_ref).consistent)

    def test_reported_operating_assets_liabilities_section_is_reconciled_with_disclosure(self):
        text = self.task.document.document_bytes.decode().replace('<tr><td>Accounts receivable',
            '<tr><td>Changes in operating assets and liabilities:</td></tr><tr><td>Accounts receivable')
        text = text.replace('</body>', '<h2>Operating activities</h2><p>Net cash provided by operating activities '
            'fell in 2025 compared with 2024. The decrease was primarily due to a net decrease of USD60.0 million '
            'in changes in working capital.</p></body>')
        task = replace(self.task,document=replace(self.task.document,document_bytes=text.encode()))
        a = FinAuditGate(artifact_root=self.root).run(task).report['analysis']
        self.assertEqual('-60000000.00',a['operating_assets_liabilities']['change'])
        self.assertTrue(a['management_check']['within_rounding'])
        mismatched = replace(task, document=replace(task.document, document_bytes=task.document.document_bytes.replace(b'USD60.0',b'USD10.0')))
        other = FinAuditGate(artifact_root=self.root).run(mismatched).report['analysis']
        self.assertFalse(other['management_check']['within_rounding'])
        self.assertEqual('-60000000.00',other['management_check']['calculated_change'])

    def test_other_period_source_does_not_fill_missing_comparative(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = re.sub(r'<ix:nonFraction id="ar24".*?</ix:nonFraction>', '—', text)
        text = text.replace('</body>', '<p><ix:nonFraction name="us-gaap:IncreaseDecreaseInAccountsReceivable" '
            'contextRef="fy2025" unitRef="money" scale="6" decimals="-6">50</ix:nonFraction></p></body>')
        task = replace(self.task,document=replace(self.task.document,document_bytes=text.encode()))
        a = FinAuditGate(artifact_root=self.root).run(task).report['analysis']
        self.assertIsNone(next(d for d in a['drivers'] if d['label']=='Accounts receivable')['comparison'])

    def test_background_risk_is_not_promoted_by_unrelated_past_earnings_sentence(self):
        from finauditgate.core.ixbrl import Filing
        text = self.task.document.document_bytes.decode().replace('</body>', '<p>Our net income declined in 2025 '
            'due to impairment expenses. We may experience negative operating cash flow in future years '
            'if competition increases or customers reduce their orders.</p></body>')
        hits = Filing(text.encode()).search_notes('Operating cash flow',current_year=2025)
        self.assertEqual('RISK_BACKGROUND',hits[0]['evidence_role'])

    def test_background_only_model_search_falls_back_without_another_model_call(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = text.replace('>20</ix:nonFraction></td><td>', '>40</ix:nonFraction></td><td>', 1)
        text = text.replace('>90</ix:nonFraction>', '>110</ix:nonFraction>')
        text = re.sub(r'<p id="receivables-note">.*?</p>', '<p>Accounts receivable may increase if customers experience financial distress. Such credit risk could reduce future cash flows and create additional losses for the company.</p>', text)
        calls = []
        def transport(request):
            calls.append(request)
            obs=json.loads(request['messages'][1]['content'])
            driver=next(d for d in obs['available_drivers'] if d['label']=='Accounts receivable')
            return json.dumps({'message':{'content':json.dumps({'action':'SEARCH_NOTES','driver_id':driver['driver_id']})}}).encode()
        task=replace(self.task,strategy='adaptive',document=replace(self.task.document,document_bytes=text.encode()))
        out=FinAuditGate(artifact_root=self.root,investigator=OllamaCashflowPlanner(transport=transport)).run(task)
        self.assertEqual(1,len(calls))
        self.assertEqual('RULE_FALLBACK',out.report['steps'][1]['choice_origin'])
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(out.run_ref).consistent)

    def test_old_period_and_conditional_text_never_become_current_explanation(self):
        from finauditgate.core.ixbrl import Filing
        text = self.task.document.document_bytes.decode().replace('</body>',
            '<h2>Operating Activities 2025</h2><p>Net cash used in operating activities was lower in 2023 than 2022. '
            'This change was mainly due to an increase in inventory held for our retail customers.</p>'
            '<p>In 2025 our operating cash flow may decrease because customers could delay payments. '
            'Future cash flows might be adversely affected if the collection of receivables slows.</p></body>')
        hits = Filing(text.encode()).search_notes('Operating cash flow', current_year=2025)
        self.assertEqual({'OTHER_PERIOD','RISK_BACKGROUND'}, {h['evidence_role'] for h in hits})

    def test_source_only_calculation_uses_presentation_sign_and_replays(self):
        gate = FinAuditGate(artifact_root=self.root)
        outcome = gate.run(self.task)
        a = outcome.report["analysis"]
        self.assertEqual("COMPLETED", a["status"])
        self.assertEqual("DIVERGING", a["direction"])
        self.assertEqual("-40000000.00", a["metrics"]["operating_cashflow"]["change"])
        self.assertEqual("-60000000.00", a["metrics"]["cash_minus_profit"]["change"])
        self.assertEqual("-60000000.00", a["drivers"][0]["change"])
        self.assertEqual("0", a["metrics"]["unexplained_residual"]["current"])
        self.assertEqual("CANDIDATE_DISCLOSURES_FOUND", outcome.report["steps"][0]["feedback"])
        self.assertEqual("AWAITING_REVIEW", outcome.report["review_status"])
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(outcome.run_ref).consistent)

    def test_amount_change_is_derived_from_new_source_without_answer_profile(self):
        task = self.changed('>120</ix:nonFraction>', '>121</ix:nonFraction>')
        a = FinAuditGate(artifact_root=self.root).run(task).report["analysis"]
        self.assertEqual("121000000", a["metrics"]["profit"]["current"])
        self.assertEqual("-1000000", a["metrics"]["unexplained_residual"]["current"])

    def test_parent_income_is_not_substituted_for_consolidated_profit(self):
        task = self.changed('name="us-gaap:ProfitLoss"', 'name="us-gaap:NetIncomeLoss"')
        a = FinAuditGate(artifact_root=self.root).run(task).report["analysis"]
        self.assertEqual({}, a["metrics"])
        self.assertIn("CONSOLIDATED_CASHFLOW_STATEMENT_NOT_FOUND", a["issues"])

    def test_one_sided_adjustment_is_retained_without_imputing_missing_zero(self):
        text = self.task.document.document_bytes.decode()
        import re
        text = re.sub(r'<ix:nonFraction id="ar24".*?</ix:nonFraction>', '—', text)
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.encode()))
        outcome = FinAuditGate(artifact_root=self.root).run(task)
        driver = outcome.report["analysis"]["drivers"][0]
        self.assertEqual("Accounts receivable", driver["label"])
        self.assertEqual("-50000000", driver["current"])
        self.assertIsNone(driver["comparison"])
        self.assertIsNone(driver["change"])
        self.assertEqual("PARTIAL", outcome.report["analysis"]["status"])
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(outcome.run_ref).consistent)

    def test_wrong_currency_entity_and_post_cutoff_do_not_produce_metrics(self):
        for task in (replace(self.task, currency="CNY"),
                     self.changed('>0001111111</xbrli:identifier>', '>0002222222</xbrli:identifier>'),
                     replace(self.task, cutoff=date(2026, 2, 1))):
            a = FinAuditGate(artifact_root=self.root).run(task).report["analysis"]
            self.assertEqual({}, a["metrics"])

    def test_dimensional_facts_are_excluded(self):
        task = self.changed('</xbrli:entity>', '<xbrli:segment><xbrldi:explicitMember>Segment A</xbrldi:explicitMember></xbrli:segment></xbrli:entity>')
        a = FinAuditGate(artifact_root=self.root).run(task).report["analysis"]
        self.assertEqual({}, a["metrics"])

    def test_row_label_parentheses_survive_numeric_cell_punctuation(self):
        task = self.changed('<td>Depreciation</td>', '<td>Depreciation (expense)</td>')
        a = FinAuditGate(artifact_root=self.root).run(task).report['analysis']
        self.assertIn('Depreciation (expense)', [d['label'] for d in a['drivers']])

    def test_missing_note_evidence_does_not_become_an_explanation(self):
        import re
        text = self.task.document.document_bytes.decode()
        text = re.sub(r'<p id="receivables-note">.*?</p>', '', text)
        task = replace(self.task, document=replace(self.task.document, document_bytes=text.encode()))
        record = FinAuditGate(artifact_root=self.root).run(task).report
        self.assertEqual('NO_MATCHING_DISCLOSURE',record['steps'][0]['feedback'])
        self.assertEqual([],record['steps'][0]['hits'])

    def test_note_search_keeps_tagged_financial_narrative_and_rejects_accounts_boilerplate(self):
        from finauditgate.core.ixbrl import Filing
        text = self.task.document.document_bytes.decode()
        note = ('<p id="tagged-note">In 2025 accounts receivable increased mainly due to extended payment terms. '
                '<ix:nonFraction contextRef="fy2025" unitRef="money" name="us-gaap:AccountsReceivableNetCurrent">50</ix:nonFraction>'
                ' million remained outstanding and will require collection in the following reporting period.</p>')
        irrelevant = '<p>Our customer accounts and passwords are stored in a protected environment. These accounts may contain sensitive personal information and require additional security controls.</p>'
        filing = Filing(text.replace('</body>',note+irrelevant+'</body>').encode())
        hits = filing.search_notes('Accounts receivable', current_year='2025')
        self.assertIn('50 million remained',hits[0]['text'])
        self.assertFalse(any('passwords' in h['text'] for h in hits))

    def test_same_direction_case_is_not_labelled_divergent(self):
        task = self.changed('>90</ix:nonFraction>', '>150</ix:nonFraction>')
        a = FinAuditGate(artifact_root=self.root).run(task).report['analysis']
        self.assertEqual('SAME_DIRECTION_OR_FLAT',a['direction'])
        self.assertIn('CASHFLOW_BRIDGE_UNRECONCILED_CURRENT',a['issues'])

    def test_unsupported_scale_transform_and_duplicate_context_fail_closed(self):
        for old, new in [('scale="6"', 'scale="80"'),
                         ('ixt:num-dot-decimal', 'ixt:num-comma-decimal')]:
            a = FinAuditGate(artifact_root=self.root).run(self.changed(old, new)).report["analysis"]
            self.assertEqual({}, a["metrics"])
            self.assertTrue(a["issues"])
        with self.assertRaisesRegex(ValueError, "DUPLICATE_CONTEXT_ID"):
            FinAuditGate(artifact_root=self.root).run(self.changed('id="fy2024"', 'id="fy2025"'))

    def test_context_does_not_inherit_ambient_decimal_traps(self):
        with localcontext() as ctx:
            ctx.prec = 3
            ctx.traps[Inexact] = ctx.traps[Rounded] = True
            outcome = FinAuditGate(artifact_root=self.root).run(self.task)
            self.assertEqual("COMPLETED", outcome.report["analysis"]["status"])

    def test_model_chooses_only_ids_and_feedback_is_present_on_second_turn(self):
        requests = []
        def transport(request):
            requests.append(request)
            obs = json.loads(request["messages"][1]["content"])
            action = ({"action": "SEARCH_NOTES", "driver_id": obs["available_drivers"][0]["driver_id"]}
                      if len(requests) == 1 else {"action": "FINISH", "driver_id": "none"})
            return json.dumps({"message": {"content": json.dumps(action)}}).encode()
        planner = OllamaCashflowPlanner(transport=transport)
        outcome = FinAuditGate(artifact_root=self.root, investigator=planner).run(replace(self.task, strategy="adaptive"))
        self.assertEqual(2, len(requests))
        obs = json.loads(requests[1]["messages"][1]["content"])
        self.assertTrue(obs["previous_searches"][0]["hits"])
        self.assertNotIn("expected", json.dumps(requests).lower())
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(outcome.run_ref).consistent)

    def test_unauthorized_tool_action_stops_and_does_not_execute(self):
        raw = json.dumps({"message": {"content": json.dumps({"action": "SHELL", "driver_id": "rm anything"})}}).encode()
        outcome = FinAuditGate(artifact_root=self.root, investigator=OllamaCashflowPlanner(transport=lambda _: raw)).run(replace(self.task, strategy="adaptive"))
        self.assertEqual(1, len(outcome.report["steps"]))
        self.assertEqual("ACTION_NOT_ALLOWED", outcome.report["steps"][0]["feedback"])
        self.assertTrue(FinAuditGate(artifact_root=self.root).replay(outcome.run_ref).consistent)

    def test_source_and_record_tampering_fail_replay(self):
        gate = FinAuditGate(artifact_root=self.root)
        outcome = gate.run(self.task)
        p = self.root / "blobs/sha256" / outcome.document_sha256
        original = p.read_bytes()
        p.write_bytes(original + b" ")
        self.assertFalse(gate.replay(outcome.run_ref).consistent)
        p.write_bytes(original)
        record = self.root / "runs" / outcome.run_ref.run_id / "cashflow.json"
        record.write_bytes(record.read_bytes() + b" ")
        self.assertFalse(gate.replay(outcome.run_ref).consistent)

    def test_private_root_is_required(self):
        with self.assertRaises(ValueError):
            FinAuditGate(artifact_root=self.workspace / "finaudit-gate/runs").run(self.task)

    def test_application_crosses_run_replay_and_returns_escaped_workpaper(self):
        from finauditgate.application import ApplicationError
        app = FinResearchOps(artifact_root=self.root)
        task = replace(self.task, document=replace(self.task.document, source_id='<script>alert("x")</script>'))
        view = app.handle(InvestigateCashflow(task))
        self.assertEqual("AWAITING_REVIEW", view.status)
        page = Path(view.workpaper_paths[0]).read_text()
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)
        reopened = FinResearchOps(artifact_root=self.root).read_case(view.case_ref)
        self.assertEqual(view, reopened)
        self.assertTrue(app.handle(ReplayRun(view.run_refs[0])).consistent)
        Path(view.workpaper_paths[0]).write_text(page + 'tampered')
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)

    def test_cli_manifest_reads_acquired_source_and_rejects_changed_bytes(self):
        from finauditgate.cli import main
        from finauditgate.core.artifacts import sha256_hex
        from contextlib import redirect_stdout, redirect_stderr
        import io
        source = self.workspace / "private/synthetic.html"
        source.write_bytes(self.task.document.document_bytes)
        provenance = source.with_suffix('.provenance.json')
        provenance.write_text(json.dumps({"data_class":"PUBLIC_SOURCE_LOCAL","form":"20-F",
            "stored_as":source.name,"issuer":"Synthetic Aurora", "source_url":self.task.source_url,
            "accession":self.task.accession,"cik":self.task.entity_identifier,
            "publication_date_filed":"2026-03-01","reporting_period_end":"2025-12-31",
            "sha256":sha256_hex(source.read_bytes())}))
        args = ['--artifact-root',str(self.root),'investigate-cashflow','--source-manifest',str(provenance),
                '--comparison-end','2024-12-31','--cutoff','2026-03-02','--currency','USD']
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0,main(args))
        self.assertTrue(Path(json.loads(out.getvalue())['workpaper']).exists())
        source.write_bytes(source.read_bytes()+b' ')
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(2,main(args))
        self.assertIn('ACQUIRED_SOURCE_HASH_MISMATCH',err.getvalue())


if __name__ == "__main__":
    unittest.main()
