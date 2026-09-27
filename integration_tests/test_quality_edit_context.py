"""Copied outside context is metadata only when every outside byte is retained."""
import json
import unittest
import test_thesis_quality_flow as flow


class ContextLLM(flow.QualityLLM):
    change_outside: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get('synthetic_schema')
        if schema and schema.__name__ in ('QualityReview', 'QualityRevision'):
            value = json.loads(result.generations[0].message.content)
            if schema.__name__ == 'QualityReview':
                value['findings'][0]['original_text'] = '经营判断保持'
            elif self.change_outside:
                value['edits'][0]['replacement_text'] = value['edits'][0]['replacement_text'].replace('尚未披露', '已经证实')
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class QualityEditContextTest(unittest.TestCase):
    setUp = flow.ThesisQualityFlowTest.setUp
    run_case = flow.ThesisQualityFlowTest.run_case

    def test_unchanged_context_can_surround_the_exact_problem_span(self):
        view, app, _ = self.run_case(ContextLLM())
        self.assertEqual('COMPLETED', view.review['status'])
        self.assertEqual(flow.CAUTIOUS_SUMMARY, view.review['effective']['final_report']['summary']['text'])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_surrounding_unrelated_content_must_stay_byte_identical(self):
        view, app, _ = self.run_case(ContextLLM(change_outside=True))
        self.assertEqual('PARTIAL', view.review['status'])
        self.assertIsNone(view.review['effective'])
        self.assertEqual(flow.SUMMARY, view.latest_report['final_report']['summary']['text'])
        self.assertEqual(view.latest_report, app.read_case(view.case_ref).latest_report)
