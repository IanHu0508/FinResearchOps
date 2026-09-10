from dataclasses import replace
from datetime import date
import json
import unittest

from finauditgate import FinAuditGate
from finauditgate.research import FundamentalEvidenceTask,ReviewNativeJudgment
from finauditgate.core.judgment import render_checked
import test_interim_research as interim_support
from test_security_market import synthetic_market_task


def fact(ident,ref,measure,relation='VALUE'):
    return {'id':ident,'kind':'FACT','refs':[ref],'measure':measure,'relation':relation,'topic':'NONE','stance':'NONE'}


class JudgmentTest(unittest.TestCase):
    setUp=interim_support.InterimResearchTest.setUp

    def sources(self):
        self.gate=FinAuditGate(artifact_root=self.root/'core')
        f=self.gate.run(FundamentalEvidenceTask(self.task))
        m=synthetic_market_task(as_of=self.task.cutoff,session=date(2026,9,4))
        m=replace(m,identity_filing=replace(m.identity_filing,currency='CNY'))
        market=self.gate.run(m)
        return f.run_ref,market.run_ref

    def test_four_financial_errors_are_rejected_and_correct_alternatives_survive(self):
        f,m=self.sources()
        claims=[fact('c1','metric:contract_balance','BALANCE','TURNED_NEGATIVE'),
                fact('c2','rule:revenue_recognition','RECOGNITION','GENERATES_CASH'),
                fact('c3','context:holdings','HOLDINGS','ZERO'),
                fact('c4','quote:2026-09-04:Close','PRICE_LOW'),
                fact('c5','metric:contract_balance','BALANCE','DECREASED'),
                fact('c6','rule:revenue_recognition','RECOGNITION','NO_NEW_CASH'),
                fact('c7','context:holdings','HOLDINGS','UNKNOWN'),
                fact('c8','quote:2026-09-04:Low','PRICE_LOW')]
        out=self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',{'claims':claims,'decisions':[]}))
        self.assertEqual([False]*4+[True]*4,[c['supported'] for c in out.report['claims']])
        self.assertTrue(self.gate.replay(out.run_ref).consistent)
        text=render_checked(out.report)
        self.assertIn('较比较值下降',text)
        self.assertIn('日内最低价：19.00',text)
        self.assertNotIn('日内最低价：20.50',text)

    def test_manager_cannot_promote_rejected_claim_and_missing_vote_is_not_silent_acceptance(self):
        f,m=self.sources()
        earlier=self.gate.run(ReviewNativeJudgment(f,m,'Bear Researcher',{'claims':[
            fact('c1','context:holdings','HOLDINGS','ZERO'),fact('c2','metric:contract_balance','BALANCE','DECREASED')],'decisions':[]}))
        proposal={'claims':[fact('c1','context:valuation','VALUATION','UNKNOWN')],
            'decisions':[{'target':earlier.run_ref.run_id+':c1','decision':'USE'}]}
        out=self.gate.run(ReviewNativeJudgment(f,m,'Research Manager',proposal,(earlier.run_ref,)))
        self.assertEqual(['REJECT','DEFER'],[d['effective'] for d in out.report['adjudications']])
        self.assertEqual(['UNSUPPORTED_CLAIM_CANNOT_BE_USED','MISSING_MANAGER_DECISION'],out.report['issues'])
        self.assertTrue(self.gate.replay(out.run_ref).consistent)

    def test_hypotheses_remain_conditional_and_need_relevant_evidence(self):
        f,m=self.sources()
        hypothesis={'id':'c1','kind':'HYPOTHESIS','refs':['metric:contract_balance'],'measure':'HYPOTHESIS',
            'relation':'UNKNOWN','topic':'CONTRACT_CAUSE','stance':'CHALLENGE'}
        out=self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',{'claims':[hypothesis],'decisions':[]}))
        self.assertIn('待验证假设',out.report['claims'][0]['text'])
        bad={**hypothesis,'refs':['context:holdings']}
        rejected=self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',{'claims':[bad],'decisions':[]}))
        self.assertFalse(rejected.report['claims'][0]['supported'])

    def test_unknown_reference_duplicate_claim_and_prior_tampering_fail(self):
        f,m=self.sources()
        out=self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',{'claims':[fact('c1','invented','BALANCE')],'decisions':[]}))
        self.assertEqual('REFERENCE_NOT_AVAILABLE',out.report['issues'][0])
        with self.assertRaisesRegex(ValueError,'ID_DUPLICATE'):
            self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',{'claims':[fact('c1','context:holdings','HOLDINGS','UNKNOWN')]*2,'decisions':[]}))
        p=self.root/'core/runs'/out.run_ref.run_id/'judgment.json'
        r=json.loads(p.read_text());r['claims'][0]['supported']=True;p.write_text(json.dumps(r))
        self.assertFalse(self.gate.replay(out.run_ref).consistent)
        with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):
            self.gate.run(ReviewNativeJudgment(f,m,'Research Manager',{'claims':[fact('c1','context:holdings','HOLDINGS','UNKNOWN')],'decisions':[]},(out.run_ref,)))

    def test_source_replay_alone_does_not_approve_unavailable_financial_facts(self):
        _,m=self.sources()
        invalid=replace(self.task,document=replace(self.task.document,document_bytes=self.task.document.document_bytes.replace(
            b'<td>Current income tax</td><td>24</td><td>27</td>',b'<td>Current income tax</td><td>24</td><td>28</td>')))
        f=self.gate.run(FundamentalEvidenceTask(invalid))
        self.assertTrue(self.gate.replay(f.run_ref).consistent)
        self.assertFalse(f.report['analysis']['metrics'])
        with self.assertRaisesRegex(ValueError,'JUDGMENT_FINANCIAL_INPUT_UNAVAILABLE'):
            self.gate.run(ReviewNativeJudgment(f.run_ref,m,'Bull Researcher',{'claims':[fact('c1','context:holdings','HOLDINGS','UNKNOWN')],'decisions':[]}))

    def test_price_rounding_does_not_depend_on_caller_decimal_rounding(self):
        from decimal import localcontext,ROUND_DOWN
        f,_=self.sources()
        # Put sub-cent prices in earlier rows while leaving the verified final
        # quote untouched, then obtain a new market record through run().
        source=synthetic_market_task(as_of=self.task.cutoff,session=date(2026,9,4))
        source=replace(source,identity_filing=replace(source.identity_filing,currency='CNY'))
        # Modify a recent non-final row so it appears in the bounded catalog.
        lines=source.ohlcv_csv.splitlines();lines[-2]=lines[-2].replace(b',20,21,',b',20.009,21,')
        source=replace(source,ohlcv_csv=b'\n'.join(lines)+b'\n')
        m2=self.gate.run(source).run_ref
        task=ReviewNativeJudgment(f,m2,'Bull Researcher',{'claims':[fact('c1','context:holdings','HOLDINGS','UNKNOWN')],'decisions':[]})
        out=self.gate.run(task)
        with localcontext() as ctx:
            ctx.rounding=ROUND_DOWN
            self.assertEqual(out.run_ref,self.gate.run(task).run_ref)
            self.assertTrue(self.gate.replay(out.run_ref).consistent)

    def test_cached_dependency_still_checks_each_parent_edge_order(self):
        from finauditgate.core.artifacts import canonical_json_bytes,sha256_hex,write_once
        from finauditgate.contracts import RunRef
        f,m=self.sources()
        proposal={'claims':[fact('c1','context:holdings','HOLDINGS','UNKNOWN')],'decisions':[]}
        bear=self.gate.run(ReviewNativeJudgment(f,m,'Bear Researcher',proposal))
        bull=self.gate.run(ReviewNativeJudgment(f,m,'Bull Researcher',proposal))
        # A correctly hashed forgery: Bull claims to have read future Bear.
        record=json.loads(canonical_json_bytes(bull.report))
        record['task']['prior_refs']=[bear.run_ref.run_id]
        raw=canonical_json_bytes(record);forged=RunRef(sha256_hex(raw))
        write_once(self.root/'core/runs'/forged.run_id/'judgment.json',raw)
        self.assertFalse(self.gate.replay(forged).consistent)
        for refs in ((forged,bear.run_ref),(bear.run_ref,forged)):
            with self.subTest(refs=refs),self.assertRaisesRegex(ValueError,'JUDGMENT_PRIOR_CONTEXT_INVALID'):
                self.gate.run(ReviewNativeJudgment(f,m,'Research Manager',proposal,refs))


if __name__=='__main__':unittest.main()
