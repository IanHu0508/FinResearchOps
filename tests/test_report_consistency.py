"""Time labels and program-owned input deltas must not certify model prose."""
from copy import deepcopy
import unittest
from test_research_delivery import context, pending_numbers, report, sources
from test_research_numbers import inputs, assumption
from finauditgate.core.forward_revision import apply_forward_revision


def changed_inputs():
    draft, _ = inputs()
    first = draft['scenarios'][0]
    first['noncontrolling_attribution']['amount'] = assumption(3.0)
    third = deepcopy(first)
    third['scenario_id'] = 'F3'
    third['noncontrolling_attribution']['amount'] = assumption(4.0)
    draft['scenarios'].append(third)
    changes = [{'scenario_id':sid,'field':'noncontrolling_attribution',
        'expected_before':{'nature':'profit','amount':old},
        'replacement':{'nature':'profit','amount':assumption(new)},
        'correction_basis':'accounting_correction','reason':'模型拟采用另一组假设，尚未获验证。','evidence_refs':['S01']}
        for sid,old,new in [('F1',3.0,7.0),('F3',4.0,8.0)]]
    return draft, changes


class ReportConsistencyTest(unittest.TestCase):
    def test_quarters_and_declared_horizon_are_labels_in_normal_sentences(self):
        for text in ['Q3单季收入回升。', '2025Q4与2026年前三季尚未观察。',
                     '作为12个月中性参照。', '本报告的12个月期限。', '不外推至12个月。', '影响使12个月风险收益变化。']:
            with self.subTest(text=text):
                value, ctx = context(report(text))
                self.assertEqual(text, ctx.block(value['summary'])['text'])
        for text in ['EPS为Q3', 'Q4%', 'Q3美元', '收益为2025Q4元', '目标价提升至12个月']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'UNBOUND_RESEARCH_NUMBER'):
                context(report(text))
        for text, token in [('Q40数据', 'Q40'), ('Q5单季收入', 'Q5')]:
            with self.subTest(text=text):
                self.assertEqual([token], pending_numbers(context(report(text))[1]))

    def test_source_proven_technical_version_is_not_a_financial_number(self):
        bundle = sources('SYNTHETIC product uses UFS 4.1; not a financial amount.')
        for text in ['UFS4.1产品导入仍需观察。', '新UFS 4.1版本已在资料中出现。']:
            with self.subTest(text=text):
                value, ctx = context(report(text), bundle)
                self.assertEqual(text, ctx.block(value['summary'])['text'])
        for text in ['EPS为UFS4.1', '价格为UFS4.1', 'UFS4.1美元', 'UFS4.1利润率99%']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'UNBOUND_RESEARCH_NUMBER'):
                context(report(text), bundle)
        # A version not proven by a research source is not accepted as bound.
        self.assertEqual(['UFS9.9'], pending_numbers(context(report('UFS9.9已量产。'), bundle)[1]))
        bundle['sources'][0]['use'] = 'sensitivity'
        self.assertEqual(['UFS4.1'], pending_numbers(context(report('UFS4.1产品。'), bundle)[1]))
        for source, claimed in [('UFS4.10', 'UFS4.1'), ('UFS9.99', 'UFS9.9')]:
            with self.subTest(source=source):
                self.assertEqual([claimed], pending_numbers(context(report(claimed + '产品。'), sources('SYNTHETIC ' + source))[1]))
        value, ctx = context(report('UFS4.1产品。'), sources('SYNTHETIC product uses UFS4.1.'))
        self.assertIn('UFS4.1', ctx.block(value['summary'])['text'])

    def test_ordinary_share_ratio_literal_must_match_the_existing_common_input(self):
        text = '按每交易单位1股普通股计算。'
        value, ctx = context(report(text))
        self.assertEqual(text, ctx.block(value['summary'])['text'])
        for text in ['按每交易单位2股普通股计算。', 'EPS为每交易单位1股普通股', '每交易单位1股普通股收益99元']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'UNBOUND_RESEARCH_NUMBER'):
                context(report(text))

    def test_complete_chinese_dates_are_time_labels_not_a_numeric_escape(self):
        for text in ['截至2025年6月末，相关披露仍需核对。', '2025年6月30日的资料未完整覆盖。']:
            with self.subTest(text=text):
                value, ctx = context(report(text))
                self.assertEqual(text, ctx.block(value['summary'])['text'])
        for text in ['EPS为2025年6月末元。', '股息（2025年6月30日）美元。',
                     '2025年6月末利润率为13.98%。']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'UNBOUND_RESEARCH_NUMBER'):
                context(report(text))
        self.assertEqual(['2025', '13'], pending_numbers(context(report('2025年13月末资料缺项。'))[1]))

    def test_time_and_enumeration_literals_preserve_original_prose(self):
        for text in ['2025年上半年利润增长，2026年仍未知。', '未来12个月需复核。',
                     '盈利质量有两点较扎实：经营与披露。', '以下三点需要核查：期间、口径和来源。', '利润有两点需要注意：经营与披露。', 'EPS有两点需要核查：期间和口径。', '公司收入2025年增长快于2024年。']:
            with self.subTest(text=text):
                value, ctx = context(report(text))
                self.assertEqual(text, ctx.block(value['summary'])['text'])

    def test_time_or_enumeration_cannot_hide_financial_values(self):
        for text in ['2025年EPS为1.23元。', '现金2025万元。', '利润率12%。', 'EPS=2025年',
                     '目标价为12个月', 'EPS为两点五元。', '利润提高两点。',
                     'EPS=Q4美元', '2025年万元', '2025.5年', '2025年收益999元。', 'EPS为（2025年）美元。', 'EPS为“2025年”美元。', 'EPS为「2025年」美元。', 'EPS（2025年）USD。', '目标价提升至12个月。', '利润率提高幅度有两点较去年更多。']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'UNBOUND_RESEARCH_NUMBER'):
                context(report(text))
        self.assertEqual(['6'], pending_numbers(context(report('未来6个月'))[1]))

    def test_change_facts_keep_versions_and_unchanged_dividends_separate(self):
        from finauditgate.application.research_changes import parameter_change_facts
        draft, changes = changed_inputs()
        before = deepcopy(draft)
        facts = parameter_change_facts(draft, changes)
        rows = {(r['scenario_id'],r['field']):r for r in facts['inputs']}
        self.assertEqual({'nature':'profit','amount':4.0},rows['F3','noncontrolling_attribution']['before'])
        self.assertEqual({'nature':'profit','amount':3.0},rows['F1','noncontrolling_attribution']['before'])
        self.assertEqual({'nature':'profit','amount':7.0},rows['F1','noncontrolling_attribution']['after'])
        for sid in ['F1','F3']:
            row=rows[sid,'cash_dividend_per_traded_unit']
            self.assertEqual(row['before'],row['after'])
            self.assertFalse(row['value_changed'])
        self.assertEqual(draft,before)
        self.assertEqual('MODEL_INPUT_COMPARISON_NOT_FACT_CERTIFICATION',facts['scope'])

    def test_change_facts_do_not_turn_rationale_edits_or_unknowns_into_numeric_changes(self):
        from finauditgate.application.research_changes import parameter_change_facts
        draft,_=inputs()
        draft['scenarios'][0]['exit_pe']['value']=None
        changed=deepcopy(draft['scenarios'][0]['exit_pe']); changed['reason']='依据仍不明确。'
        changes=[{'scenario_id':'F1','field':'exit_pe','expected_before':None,'replacement':changed,
                  'correction_basis':'assumption_update','reason':'依据修订。','evidence_refs':['S01']}]
        row=next(r for r in parameter_change_facts(draft,changes)['inputs'] if r['field']=='exit_pe')
        self.assertFalse(row['value_changed']); self.assertTrue(row['rationale_changed'])
        self.assertIsNone(row['before']); self.assertIsNone(row['after'])
        changes[0]['expected_before']=99
        with self.assertRaises(ValueError): parameter_change_facts(draft,changes)

    def test_same_amount_attribution_direction_is_visible_in_each_version(self):
        from finauditgate.application.research_changes import render_parameter_facts
        draft,_=inputs()
        before=draft['scenarios'][0]['noncontrolling_attribution']
        after=deepcopy(before); after['nature']='loss'
        changes=[{'scenario_id':'F1','field':'noncontrolling_attribution',
                  'expected_before':{'nature':'profit','amount':before['amount']['value']},
                  'replacement':after,'correction_basis':'assumption_update','reason':'方向假设调整。','evidence_refs':['S01']}]
        applied=apply_forward_revision(draft,changes)
        text='\n'.join(render_parameter_facts({'forward_draft':draft,'forward_revision':{'changes':changes},**applied}))
        self.assertIn('盈利',text); self.assertIn('亏损',text)
