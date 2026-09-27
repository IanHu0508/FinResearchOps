"""Program comparison reaches the model and survives report/reader boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from unittest import TestCase

import test_thesis_delivery as delivery
from finauditgate.application.thesis_case import validate
from finauditgate.application.research_changes import parameter_change_facts


class MisleadingExplanationLLM(delivery.SelectionLLM):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get('synthetic_schema')
        if schema and schema.__name__ == 'FinalResearchReport':
            payload = json.loads(messages[-1].content)
            assert payload['parameter_change_facts']['scope'] == 'MODEL_INPUT_COMPARISON_NOT_FACT_CERTIFICATION'
            value = json.loads(result.generations[0].message.content)
            value['summary']['text'] += '2025年经营观察仍需复核，未来12个月有两点需要注意：经营与估值。'
            value['change_explanations'][0]['explanation']['text'] = '原上行情景低于正常情景，因此原假设错误。'
            value['belief_explanations'][0]['explanation']['text'] = '每股分红已经随归母盈利变化自动下降。'
            result.generations[0].message.content = json.dumps(value,ensure_ascii=False)
        return result


class LiteralBoundaryLLM(delivery.SelectionLLM):
    injected_text: str = ''

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result=super()._generate(messages,stop=stop,run_manager=run_manager,**kwargs)
        schema=kwargs.get('synthetic_schema')
        if schema and schema.__name__=='FinalResearchReport':
            value=json.loads(result.generations[0].message.content)
            value['summary']['text']=self.injected_text
            result.generations[0].message.content=json.dumps(value,ensure_ascii=False)
        return result


class ReportConsistencyFlowTest(TestCase):
    setUp = delivery.ThesisDeliveryTest.setUp
    run_case = delivery.ThesisDeliveryTest.run_case

    def test_actual_changes_reach_final_input_and_model_reasons_stay_reviewable(self):
        view, app = self.run_case(MisleadingExplanationLLM())
        record = view.latest_report
        last = record['exchanges'][-1]
        payload = json.loads(last['messages'][1]['content'])
        self.assertEqual(parameter_change_facts(record['forward_draft'],record['forward_revision']['changes']),
                         payload['parameter_change_facts'])
        dividends=[r for r in payload['parameter_change_facts']['inputs'] if r['field']=='cash_dividend_per_traded_unit']
        self.assertTrue(dividends); self.assertTrue(all(not r['value_changed'] for r in dividends))
        report = Path(view.report_path).read_text()
        appendix = Path(view.report_path).with_name('process-record.md').read_text()
        self.assertIn('参数前后对账（程序生成）',report)
        self.assertIn('数值未改',report)
        self.assertIn('2025年经营观察',report)
        for text in ['原上行情景低于正常情景，因此原假设错误。','每股分红已经随归母盈利变化自动下降。']:
            self.assertNotIn(text,report)
            self.assertIn(text,appendix)
            self.assertIn(text,json.dumps(record['final_report'],ensure_ascii=False))
        self.assertIn('模型修改理由与信念解释（待核）',appendix)
        for section in ['经营表现与持续性','盈利质量与归母勾稽','现金创造与资本配置','估值与当前价格要求','最强反证']:
            self.assertIn(section,report)
        self.assertEqual(13,len(record['exchanges']))
        self.assertEqual(view,app.read_case(view.case_ref))

    def test_saved_model_input_cannot_forge_the_program_comparison(self):
        view,_=self.run_case()
        record=deepcopy(view.latest_report)
        last=record['exchanges'][-1]
        payload=json.loads(last['messages'][1]['content'])
        payload['parameter_change_facts']['inputs'][0]['before']=999
        changed=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        last['messages'][1]['content']=changed
        call=next(c for c in record['model_calls'] if any(o.get('id')==last['response_id'] for o in c.get('output',[])))
        next(m for m in call['messages'][0] if m['type']=='human')['content']=changed
        with self.assertRaisesRegex(ValueError,'THESIS_EFFECTIVE_NUMBERS_NOT_DELIVERED'):
            validate(record)

    def test_review_financial_bypasses_are_refused_by_the_full_graph(self):
        from finauditgate.application import ApplicationError
        for text in ['EPS为（2025年）美元。', 'EPS为“2025年”美元。', 'EPS为「2025年」美元。', 'EPS（2025年）USD。','目标价提升至12个月。','利润率提高幅度有两点较去年更多。']:
            with self.subTest(text=text), self.assertRaises(ApplicationError):
                self.run_case(LiteralBoundaryLLM(injected_text=text))
        self.assertFalse(list(self.root.glob('application/thesis-cases/*/case.json')))

    def test_financial_topic_can_still_introduce_a_real_list_or_year_label(self):
        view,app=self.run_case(LiteralBoundaryLLM(injected_text='公司收入2025年增长快于2024年。EPS有两点需要核查：期间和口径。'))
        self.assertIn('EPS有两点需要核查',Path(view.report_path).read_text())
        self.assertEqual(view,app.read_case(view.case_ref))
