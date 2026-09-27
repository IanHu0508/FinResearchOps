"""One no-answer transport retry may precede one content-preserving reason fill."""
from copy import deepcopy
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import httpx
from openai import APIConnectionError
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.application import ApplicationError
from finauditgate.application.thesis_case import validate
from finauditgate.adapters.thesis_recovery import repair_messages
import test_thesis_recovery_flow as existing


class CombinedLLM(existing.RecoveryLLM):
    target: str = 'Bull Researcher:RevisionBrief'
    combo_enabled: bool = True

    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        schema=kwargs.get('synthetic_schema');kind=schema.__name__ if schema else ''
        key=json.loads(messages[-1].content).get('node','')+':'+kind
        n=self.stage_calls.get(key,0)+1
        if self.combo_enabled and key==self.target and n==1:
            self.stage_calls[key]=n
            raise APIConnectionError(request=httpx.Request('POST','https://synthetic.invalid'))
        result=super()._generate(messages,stop=stop,run_manager=run_manager,**kwargs)
        if self.combo_enabled and key==self.target and n==2:
            value=json.loads(result.generations[0].message.content)
            del value['scenario_assessments' if kind=='FinalResearchReport' else 'updates'][0]['reason']
            result.generations[0].message.content=json.dumps(value,ensure_ascii=False)
        return result


class CombinedRecoveryFlowTest(TestCase):
    setUp=existing.ThesisRecoveryFlowTest.setUp
    run_case=existing.ThesisRecoveryFlowTest.run_case

    def test_transport_then_missing_reason_uses_exactly_two_extras_and_replays(self):
        budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288)
        view,app=self.run_case(CombinedLLM(),budget=budget)
        record=view.latest_report
        self.assertEqual(15,budget.calls);self.assertEqual(15,len(record['model_calls']))
        self.assertEqual(['CONNECTION','MISSING_REASON'],[r['reason'] for r in record['recovery']['attempts']])
        self.assertEqual([1,2,3],[c['thesis_stage']['attempt'] for c in record['model_calls'][2:5]])
        before=json.loads(record['model_calls'][3]['output'][0]['content'])
        after=json.loads(record['model_calls'][4]['output'][0]['content']);self.assertTrue(after['updates'][0].pop('reason'))
        self.assertEqual(before,after);self.assertEqual(view,app.read_case(view.case_ref))
        execution=next(self.root.glob('application/thesis-executions/*'))
        model=CombinedLLM(combo_enabled=False)
        replay,_=self.run_case(model,root=self.root/'again',resume=execution,
            budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288))
        self.assertEqual({},model.stage_calls);self.assertEqual(record['model_calls'],replay.latest_report['model_calls'])
        self.assertEqual(record['budget'],replay.latest_report['budget'])
        forged=deepcopy(record);forged['recovery']['attempts'][0]['reason']='EMPTY_RESPONSE'
        with self.assertRaises(ValueError):validate(forged)

    def test_pending_third_attempt_resumes_with_both_dependencies(self):
        budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288)
        with patch('finauditgate.adapters.thesis_invocation.repair_messages',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_case(CombinedLLM(),budget=budget)
        execution=next(self.root.glob('application/thesis-executions/*'))
        original=json.loads((execution/'runtime-receipt.json').read_text())
        self.assertEqual(4,budget.calls);self.assertIsNone(original['recovery']['attempts'][-1]['retry_run_id'])
        model=CombinedLLM(combo_enabled=False)
        view,app=self.run_case(model,root=self.root/'resume',resume=execution,
            budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288))
        self.assertEqual(1,model.stage_calls['Bull Researcher:RevisionBrief'])
        self.assertEqual(original['model_calls'],view.latest_report['model_calls'][:4])
        self.assertEqual(view,app.read_case(view.case_ref))

    def test_final_three_attempts_and_reassessment_keep_full_cost_chain(self):
        budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288)
        view,_=self.run_case(CombinedLLM(target='Portfolio Manager:FinalResearchReport'),budget=budget)
        self.assertEqual(['FAILED','FAILED','COMPLETED'],[x['status'] for x in view.latest_report['final_generation']])
        execution=next(self.root.glob('application/thesis-executions/*'))
        new,app=self.run_case(CombinedLLM(combo_enabled=False),root=self.root/'reassess',resume=execution,
            budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288),reassess=True)
        self.assertEqual(3,len(new.latest_report['recovery']['retired_final_calls']))
        self.assertEqual(16,new.latest_report['budget']['calls']);self.assertEqual(new,app.read_case(new.case_ref))

    def test_final_pending_third_attempt_retains_two_failed_generations(self):
        budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288)
        with patch('finauditgate.adapters.thesis_invocation.repair_messages',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_case(CombinedLLM(target='Portfolio Manager:FinalResearchReport'),budget=budget)
        execution=next(self.root.glob('application/thesis-executions/*'))
        original=json.loads((execution/'runtime-receipt.json').read_text())
        model=CombinedLLM(combo_enabled=False)
        view,app=self.run_case(model,root=self.root/'resume',resume=execution,
            budget=ModelBudget(ceiling_cny=None,max_input_bytes=524288))
        self.assertEqual({'Portfolio Manager:FinalResearchReport':1},model.stage_calls)
        self.assertEqual(original['final_generation'],view.latest_report['final_generation'][:2])
        self.assertEqual(15,view.latest_report['budget']['calls']);self.assertEqual(view,app.read_case(view.case_ref))

    def test_combo_reason_fill_may_not_change_existing_judgment(self):
        model=CombinedLLM(mutate_repair=True)
        with self.assertRaises(ApplicationError):self.run_case(model)
        self.assertEqual(3,model.stage_calls['Bull Researcher:RevisionBrief'])
        self.assertFalse(list(self.root.glob('application/thesis-cases/*/case.json')))
        execution=next(self.root.glob('application/thesis-executions/*'))
        again=CombinedLLM(combo_enabled=False)
        with self.assertRaises(ApplicationError):self.run_case(again,root=self.root/'retry',resume=execution)
        self.assertEqual({},again.stage_calls)


class CapturedTransportLLM(existing.RecoveryLLM):
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        result=super()._generate(messages,stop=stop,run_manager=run_manager,**kwargs)
        schema=kwargs.get('synthetic_schema')
        if schema and schema.__name__=='FinalResearchReport':
            from openai.types.chat import ChatCompletion
            value=json.loads(result.generations[0].message.content);value['rating']='Sell'
            error=APIConnectionError(request=httpx.Request('POST','https://synthetic.invalid'))
            error.completion=ChatCompletion.model_validate({'id':'synthetic-returned-answer','object':'chat.completion','created':0,
                'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':200,'total_tokens':300}})
            raise error
        return result


class CapturedTransportTest(TestCase):
    setUp=existing.ThesisRecoveryFlowTest.setUp
    run_case=existing.ThesisRecoveryFlowTest.run_case

    def test_transport_exception_with_captured_answer_cannot_redraw_it(self):
        model=CapturedTransportLLM()
        with self.assertRaises(ApplicationError):self.run_case(model)
        self.assertEqual(1,model.stage_calls['Portfolio Manager:FinalResearchReport'])
        ex=next(self.root.glob('application/thesis-executions/*'))
        rt=json.loads((ex/'runtime-receipt.json').read_text())
        self.assertFalse(rt['recovery']['attempts'])
        self.assertEqual('Sell',json.loads(rt['model_calls'][-1]['failure_response']['provider_response']['choices'][0]['message']['content'])['rating'])
        self.assertFalse(list(self.root.glob('application/thesis-cases/*/case.json')))
