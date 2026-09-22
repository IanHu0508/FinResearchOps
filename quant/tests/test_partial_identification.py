"""Independent finite-completion and time-boundary checks; synthetic data only."""
from dataclasses import replace
from datetime import timedelta
from itertools import permutations, product
import math
import unittest

from quant.contracts import ContractError, market_time
from quant.data.records import OutcomePrice
from quant.evaluation.rank_bounds import rank_ic_bounds
from quant.labels.intervals import percentile_intervals, squared_interval_loss, half_squared_interval_derivatives
from quant.labels.forward import labels_at_cutoff
from quant.pipeline import prepare_dataset
from quant.splits.walk_forward import EvaluationBatch, prepare_fold
from quant.synthetic import make_synthetic_data, synthetic_fold


def oracle_ranks(values):
    return [1+sum(v<x for v in values)+(sum(v==x for v in values)-1)/2 for x in values]


def oracle_ic(scores, values):
    x,y=oracle_ranks(scores),oracle_ranks(values)
    mean=(len(x)+1)/2
    scale=math.sqrt(sum((v-mean)**2 for v in x)*sum((v-mean)**2 for v in y))
    return sum((a-mean)*(b-mean) for a,b in zip(x,y))/scale if scale else None


class IdentificationTests(unittest.TestCase):
    def test_rank_intervals_keep_unknown_slots_and_average_ties(self):
        self.assertEqual(((0,1/3),(1/2,5/6),(1/2,5/6),None),percentile_intervals((1,2,2,None)))
        self.assertEqual((None,None),percentile_intervals((None,None)))
        self.assertEqual(((0,0),(1,1)),percentile_intervals((1,2)))
        # Survivor percentiles would be 0,.5,1, which are not these intervals.
        self.assertEqual(((0,1/3),(1/3,2/3),(2/3,1),None),percentile_intervals((1,2,3,None)))

    def test_finite_completions_include_unknown_known_ties_and_orders(self):
        completions=(-2,-1,0,.5,1,1.5,2,3,4)
        patterns=((0,2,None,None),(0,None,2,None),(None,0,None,2),(0,0,2,None),(2,0,0,None))
        checked=0
        for values in patterns:
            unknown=[i for i,v in enumerate(values) if v is None]
            intervals=percentile_intervals(values)
            score_patterns=list(permutations(range(4)))+[(0,0,1,2),(2,1,1,0)]
            for scores in score_patterns:
                bounds=rank_ic_bounds(scores,values)
                self.assertFalse(bounds['contains_undefined_completions'])
                for extra in product(completions,repeat=len(unknown)):
                    filled=list(values)
                    for i,v in zip(unknown,extra):filled[i]=v
                    actual=oracle_ic(scores,filled)
                    self.assertLessEqual(bounds['lower']-1e-14,actual)
                    self.assertGreaterEqual(bounds['upper']+1e-14,actual)
                    for i,rank in enumerate(oracle_ranks(filled)):
                        if intervals[i] is not None:
                            self.assertLessEqual(intervals[i][0],(rank-1)/3)
                            self.assertGreaterEqual(intervals[i][1],(rank-1)/3)
                    checked+=1
        self.assertGreater(checked,5000)

    def test_complete_data_collapses_to_point_and_does_not_use_no_tie_formula(self):
        for x,y in [((1,1,3,4),(2,1,3,4)),((4,3,2,1),(1,2,3,4)),((0,0,1,1),(2,2,3,4))]:
            bounds=rank_ic_bounds(x,y)
            self.assertAlmostEqual(oracle_ic(x,y),bounds['point'],places=14)
            self.assertEqual(bounds['lower'],bounds['upper'])
            self.assertTrue(bounds['sharp'])

    def test_degeneracy_is_explicit_not_zero_correlation(self):
        for scores,values in [((1,1),(1,2)),((1,2),(None,None)),((1,2),(1,None)),((1,2,3),(1,1,None))]:
            b=rank_ic_bounds(scores,values)
            self.assertTrue(b['contains_undefined_completions'])
            self.assertIsNone(b['lower']);self.assertIsNone(b['point'])

    def test_interval_loss_has_no_midpoint_attraction(self):
        for p in (.2,.3,.8):
            self.assertEqual(0,squared_interval_loss(p,(.2,.8)))
            self.assertEqual((0.0,0.0),half_squared_interval_derivatives(p,(.2,.8)))
        self.assertAlmostEqual(.01,squared_interval_loss(.1,(.2,.8)))
        self.assertEqual((0,1.0),half_squared_interval_derivatives(.5,(.5,.5)))
        for p in (.1,.9):
            eps=1e-6;f=lambda x:squared_interval_loss(x,(.2,.8))/2
            g,h=half_squared_interval_derivatives(p,(.2,.8))
            self.assertAlmostEqual(g,(f(p+eps)-f(p-eps))/(2*eps),places=9)
            self.assertAlmostEqual(h,(f(p+eps)-2*f(p)+f(p-eps))/eps**2,places=5)


class IntervalCutoffTests(unittest.TestCase):
    def test_censor_before_ranking_and_future_values_do_not_leak(self):
        data,spec=make_synthetic_data(score_start=60,score_end=60)
        end=data.sessions[80];cutoff=market_time(data.sessions[82],0)
        delayed=market_time(data.sessions[82],21)
        def altered(multiplier):
            return replace(data,bars=tuple(replace(b,available_at=delayed,return_close=b.return_close*multiplier)
                if b.symbol=='SYN000.SH' and b.session==end else b for b in data.bars))
        first=prepare_dataset(altered(1),spec,knowledge_cutoff=cutoff)
        second=prepare_dataset(altered(100),spec,knowledge_cutoff=cutoff)
        self.assertEqual(first,second)
        self.assertEqual(6,len(first.panel.rows))
        self.assertEqual(5,sum(y.supervised for y in first.labels))
        self.assertTrue(all(y.observed_count==5 and y.universe_size==6 for y in first.labels))
        self.assertTrue(all(y.target_percentile is None for y in first.labels))
        self.assertIsNone(first.labels[0].raw_return)
        # A latest snapshot followed by honest re-identification agrees exactly.
        latest=prepare_dataset(altered(100),spec)
        self.assertEqual(first,labels_at_cutoff(latest,cutoff))
        with self.assertRaisesRegex(ContractError,'UNCENSOR'):
            labels_at_cutoff(first,delayed+timedelta(days=1))

    def test_unknown_member_stays_in_evaluation_but_not_supervised_batch(self):
        data,spec=make_synthetic_data();window=synthetic_fold(data)
        affected_score=data.sessions[80];affected_end=data.sessions[100]
        data=replace(data,outcome_prices_enabled=True,outcome_prices=tuple(
            OutcomePrice(b.symbol,b.session,b.available_at,b.return_open,b.return_close,b.source_id,'synthetic-basis')
            for b in data.bars if not (b.symbol=='SYN000.SH' and b.session==affected_end)))
        dataset=prepare_dataset(data,spec);fold=prepare_fold(dataset,window)
        keys=[r.key.symbol for r in fold.train.rows if r.key.as_of.date()==affected_score]
        self.assertEqual(5,len(keys));self.assertNotIn('SYN000.SH',keys)
        self.assertEqual(6,sum(r.key.as_of.date()==affected_score for r in dataset.panel.rows))
        self.assertAlmostEqual(1,sum(w for r,w in zip(fold.train.rows,fold.train.weights) if r.key.as_of.date()==affected_score))
        rows=tuple(r for r in dataset.panel.rows if r.key.as_of.date()==affected_score)
        labels=tuple(y for y in dataset.labels if y.key.as_of.date()==affected_score)
        EvaluationBatch(rows,labels)
        with self.assertRaisesRegex(ContractError,'FULL_UNIVERSE'):
            EvaluationBatch(rows[1:],labels[1:])

    def test_horizon_crossing_cutoff_is_not_partial_permission_to_peek(self):
        data,spec=make_synthetic_data(score_start=60,score_end=60)
        dataset=prepare_dataset(data,spec,knowledge_cutoff=market_time(data.sessions[80],0))
        self.assertTrue(all(y.raw_return is None and y.target_interval is None for y in dataset.labels))
        self.assertEqual({'HORIZON_NOT_MATURE_AT_CUTOFF'},{y.missing_reason for y in dataset.labels})

class ProtocolBoundaryTests(unittest.TestCase):
    def test_streamed_development_default_censors_post_2024_confirmation(self):
        from quant.contracts import evaluation_cutoff
        from quant.pipeline import prepare_window_day
        data,spec=make_synthetic_data(score_start=60,score_end=60)
        window=synthetic_fold(data)
        # Place this scoring day in the test branch of a valid earlier split.
        window=replace(window,train_start=data.sessions[0],validation_start=data.sessions[20],
            validation_end=data.sessions[39],test_start=data.sessions[40],test_end=data.sessions[70])
        end=data.sessions[80];late=evaluation_cutoff('development')+timedelta(days=1,hours=21)
        data=replace(data,bars=tuple(replace(b,available_at=late)
            if b.symbol=='SYN000.SH' and b.session==end else b for b in data.bars))
        view=prepare_window_day(data,spec,window)
        self.assertEqual(evaluation_cutoff('development'),view.labels[0].knowledge_cutoff)
        self.assertEqual(5,view.labels[0].observed_count)
        self.assertIsNone(view.labels[0].raw_return)

    def test_memory_test_tail_respects_cutoff_and_missing_calendar_is_rejected(self):
        data,spec=make_synthetic_data();window=synthetic_fold(data)
        cut=market_time(data.sessions[215],0)
        dataset=prepare_dataset(data,spec)
        fold=prepare_fold(dataset,window,evaluation_knowledge_cutoff=cut)
        self.assertTrue(all(y.label_end_date<cut.date() for y in fold.test.labels))
        self.assertTrue(fold.purged_test)
        self.assertTrue(all(y.knowledge_cutoff==cut for y in fold.test.labels))
        tail_data,tail_spec=make_synthetic_data(score_end=229)
        with self.assertRaisesRegex(ContractError,'HORIZON_COVERAGE'):
            prepare_fold(prepare_dataset(tail_data,tail_spec),replace(window,test_end=tail_data.scoring_dates[-1]))

    def test_hac_preserves_calendar_gaps(self):
        from datetime import date
        from quant.evaluation.uncertainty import date_axis_hac
        days=[date(2020,1,1)+timedelta(days=i) for i in range(3)]
        report=date_axis_hac({days[0]:1.,days[2]:-1.},days,lag=20)
        self.assertEqual(2,report['observed_dates'])
        self.assertAlmostEqual(1/21,report['standard_error']**2)

    def test_selection_uses_lower_bound_not_complete_day_metric_or_upper(self):
        from copy import deepcopy
        from quant.evaluation.prediction import select_training_window
        from quant.evaluation.rank_bounds import METHOD
        from quant.contracts import TARGET_ID
        def report(lo,hi,sensitivity):
            return {'selection_eligible':True,'mean_rank_ic_lower_bound':lo,'mean_rank_ic_upper_bound':hi,
                'target_id':TARGET_ID,'bound_method':METHOD,'complete_day_sensitivity':{'mean_rank_ic':sensitivity},
                'daily':[{'as_of':'2020-01-02T21:30:00+08:00','count':100,'label_set_id':'same','knowledge_cutoff':'2024-01-01T00:00:00+08:00'}]}
        values={'rolling_2y':report(.01,.03,.9),'rolling_5y':report(.02,.025,-.9),'expanding':report(.015,.4,1.)}
        selected=select_training_window(values)
        self.assertEqual('rolling_5y',selected['selected_window'])
        self.assertFalse(selected['identified_dominance_over_others'])
        values['rolling_2y']['mean_rank_ic_lower_bound']=.02-1e-13
        self.assertEqual('rolling_2y',select_training_window(values)['selected_window'])
        values['expanding']['selection_eligible']=False
        with self.assertRaisesRegex(ContractError,'UNDEFINED_PRIMARY'):
            select_training_window(values)
