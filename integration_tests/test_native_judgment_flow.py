import json
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]/'tests'))
import test_research_workflow as support
from test_security_market import synthetic_market_task
from test_native_judgment import fact
from native_support import NativeSyntheticLLM,patched_native_runtime,synthetic_native_tools
from langchain_core.runnables import RunnableLambda
from finauditgate.application import FinResearchOps,ApplicationError
from finauditgate.adapters.tradingagents_native import NativeAuditAdapter
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.research import RunAuditedNativeResearch
from finauditgate.core.judgment import read_record
from finauditgate.contracts import RunRef


class TypedModel(NativeSyntheticLLM):
    def with_structured_output(self,schema,**kwargs):
        if schema.__name__!='NativeClaimBatch':return super().with_structured_output(schema,**kwargs)
        def parse(raw):
            value=schema.model_validate_json(raw.content)
            return {'raw':raw,'parsed':value,'parsing_error':None} if kwargs.get('include_raw') else value
        return self.bind(synthetic_schema=schema)|RunnableLambda(parse)

    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        schema=kwargs.get('synthetic_schema')
        if schema is None or schema.__name__!='NativeClaimBatch':
            result=super()._generate(messages,stop,run_manager,**kwargs)
            if result.generations[0].message.content:
                result.generations[0].message.content+=' UNSAFE_UNCHECKED_DRAFT'
            return result
        payload=json.loads(messages[-1].content);node=payload['node']
        self.requests.append({'node':node,'messages':[m.model_dump(mode='json') for m in messages]})
        proposals=[fact('c1','metric:operating_cashflow','NET_CASH_FLOW'),
            fact('c2','context:holdings','HOLDINGS','ZERO'),
            fact('c3','rule:revenue_recognition','RECOGNITION','GENERATES_CASH'),
            fact('c4','quote:2026-03-02:Close','PRICE_LOW'),
            fact('c5','context:holdings','HOLDINGS','UNKNOWN')]
        decisions=[{'target':c['target'],'decision':'USE'} for c in payload['prior_candidates']] if node in ('Research Manager','Portfolio Manager') else []
        return self._result(json.dumps({'claims':proposals,'decisions':decisions}))


class JudgmentFlowTest(unittest.TestCase):
    setUp=support.ResearchWorkflowTest.setUp

    def run_case(self,mutate=None):
        market=synthetic_market_task();data=synthetic_native_tools()
        data.tools['get_verified_market_snapshot'].func=lambda **kwargs:market.snapshot
        model=TypedModel()
        class Adapter(NativeAuditAdapter):
            def run_native(self,*args,**kwargs):
                r=super().run_native(*args,**kwargs)
                if mutate:mutate(r)
                return r
        with patched_native_runtime(model,data):
            view=FinResearchOps(artifact_root=self.root,researcher=Adapter(live=True,
                budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288,max_output_tokens=32768))).handle(
                    RunAuditedNativeResearch(self.task,'AURORA','Assess claims',market_task=market,check_judgments=True))
        return view,model

    def test_complete_native_flow_only_passes_supported_claims(self):
        view,model=self.run_case()
        self.assertEqual('finresearchops.native-audited-case/v5',view.latest_report['schema_version'])
        result=view.latest_report['result']
        self.assertEqual(16,len(result['topology_before']['nodes']))
        self.assertEqual(30,len(result['topology_before']['edges']))
        self.assertEqual(10,len(result['judgment_reviews']))
        self.assertEqual(19,len(result['model_calls']))
        for row in result['judgment_reviews']:
            r=read_record(self.root/'core',RunRef(row['run_id']))
            self.assertEqual([True,False,False,False,True],[c['supported'] for c in r['claims']])
            if row['node'] in ('Research Manager','Portfolio Manager'):
                self.assertTrue(any(d['requested']=='USE' and d['effective']=='REJECT' for d in r['adjudications']))
        for call in model.requests:
            if call['node'] not in ('Fundamentals Analyst','Market Analyst'):
                self.assertNotIn('UNSAFE_UNCHECKED_DRAFT',str(call['messages']))
        self.assertNotIn('UNSAFE_UNCHECKED_DRAFT',Path(view.report_path).read_text())
        self.assertEqual(view,FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

    def test_forged_manager_report_fails_even_with_matching_node_output(self):
        def corrupt(r):
            r['reports']['final_trade_decision']+='\nUnverified false conclusion'
            for c in r['node_calls']:
                if c['node']=='Portfolio Manager':c['output']['final_trade_decision']=r['reports']['final_trade_decision']
        with self.assertRaisesRegex(ApplicationError,'JUDGMENT_REPORT_NOT_CHECKED_PROJECTION'):
            self.run_case(corrupt)


if __name__=='__main__':unittest.main()
