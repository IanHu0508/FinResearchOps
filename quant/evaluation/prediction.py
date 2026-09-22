from collections import defaultdict
from datetime import datetime
import statistics

from quant.contracts import TARGET_ID, fingerprint, require, session_of
from quant.labels.ranks import percentiles
from quant.labels.intervals import squared_interval_loss
from .rank_bounds import METHOD, rank_ic_bounds


def spearman(left, right):
    require(all(v is not None for v in right), "POINT_SPEARMAN_REQUIRES_COMPLETE_OUTCOMES")
    return rank_ic_bounds(left,right)['point']


def _summary(daily):
    defined = [d for d in daily if d['rank_ic_lower_bound'] is not None]
    complete = [d for d in daily if d['unknown_count']==0]
    exact = [d['rank_ic'] for d in complete if d['rank_ic'] is not None]
    valid_all = len(defined)==len(daily) and bool(daily)
    losses = [d['interval_mse'] for d in daily if d['interval_mse'] is not None]
    return {'days':len(daily),'valid_days':len(defined),
        'mean_rank_ic_lower_bound':statistics.fmean(d['rank_ic_lower_bound'] for d in daily) if valid_all else None,
        'mean_rank_ic_upper_bound':statistics.fmean(d['rank_ic_upper_bound'] for d in daily) if valid_all else None,
        'mean_rank_ic_bound_width':statistics.fmean(d['rank_ic_upper_bound']-d['rank_ic_lower_bound'] for d in daily) if valid_all else None,
        'lower_endpoint_std':statistics.stdev(d['rank_ic_lower_bound'] for d in daily) if valid_all and len(daily)>1 else None,
        'upper_endpoint_std':statistics.stdev(d['rank_ic_upper_bound'] for d in daily) if valid_all and len(daily)>1 else None,
        'mean_rank_ic':statistics.fmean(exact) if valid_all and len(complete)==len(daily) else None,
        'date_equal_interval_mse':statistics.fmean(losses) if losses else None,
        'interval_loss_days':len(losses),'selection_eligible':valid_all,
        'complete_day_sensitivity':{'days':len(complete),'valid_days':len(exact),
            'mean_rank_ic':statistics.fmean(exact) if exact else None,
            'std_rank_ic':statistics.stdev(exact) if len(exact)>1 else None,
            'role':'SENSITIVITY_ONLY_SAME_MODELS_AND_PREDICTIONS_NOT_SELECTION'}}


def evaluate_predictions(predictions, evaluation, *, quantile_groups=5):
    require(type(quantile_groups) is int and quantile_groups>=2, 'QUANTILE_GROUP_COUNT_INVALID')
    expected={r.key for r in evaluation.rows}
    require(len(predictions)==len(expected) and {p.key for p in predictions}==expected,
            'PREDICTION_COVERAGE_MISMATCH')
    prediction_map={p.key:p for p in predictions}
    grouped=defaultdict(list)
    for label in evaluation.labels:grouped[label.key.as_of].append(label)
    daily=[]
    for as_of,labels in sorted(grouped.items()):
        labels.sort(key=lambda y:y.key)
        score=tuple(prediction_map[y.key].predicted_target_percentile for y in labels)
        bounds=rank_ic_bounds(score,tuple(y.raw_return for y in labels))
        losses=[squared_interval_loss(s,y.target_interval) for s,y in zip(score,labels) if y.supervised]
        bins=defaultdict(list)
        for rank,label in zip(percentiles(score),labels):
            bins[min(int(rank*quantile_groups),quantile_groups-1)].append(label.raw_return)
        quantiles=[]
        for group in range(quantile_groups):
            values=bins[group]; known=[v for v in values if v is not None]
            quantiles.append({'group':group+1,'count':len(values),'observed_count':len(known),
                'mean_forward_return':statistics.fmean(known) if values and len(values)==len(known) else None})
        low,high=quantiles[0]['mean_forward_return'],quantiles[-1]['mean_forward_return']
        daily.append({'as_of':as_of.isoformat(),'year':session_of(as_of).year,'count':len(labels),
            'observed_count':bounds['observed_count'],'unknown_count':bounds['unknown_count'],
            'knowledge_cutoff':labels[0].knowledge_cutoff.isoformat(),'label_set_id':fingerprint(tuple(labels)),
            'rank_ic':bounds['point'],'rank_ic_lower_bound':bounds['lower'],'rank_ic_upper_bound':bounds['upper'],
            'rank_ic_interval':bounds,'interval_mse':statistics.fmean(losses) if losses else None,
            'reason':bounds['reason'],'quantiles':quantiles,
            'top_minus_bottom_forward_return':high-low if high is not None and low is not None else None})
    return summarize_daily(daily,quantile_groups=quantile_groups)


def summarize_daily(daily, *, quantile_groups=5):
    require(bool(daily),'DAILY_EVALUATION_EMPTY')
    require(len({d['as_of'] for d in daily})==len(daily),'DUPLICATE_EVALUATION_DATE')
    yearly=defaultdict(list);monthly=defaultdict(list)
    for day in daily:
        session=session_of(datetime.fromisoformat(day['as_of']))
        require(day['year']==session.year,'EVALUATION_YEAR_MISMATCH')
        yearly[session.year].append(day);monthly[session.strftime('%Y-%m')].append(day)
    complete=[d for d in daily if d['unknown_count']==0]
    quantile_means=[]
    for group in range(quantile_groups):
        values=[d['quantiles'][group]['mean_forward_return'] for d in complete
                if d['quantiles'][group]['mean_forward_return'] is not None]
        quantile_means.append({'group':group+1,'days_with_members':len(values),
            'date_equal_mean_forward_return':statistics.fmean(values) if values else None})
    spreads=[d['top_minus_bottom_forward_return'] for d in complete if d['top_minus_bottom_forward_return'] is not None]
    return {'metric':'full_universe_rank_ic_outer_bounds','bound_method':METHOD,'target_id':TARGET_ID,
        'daily':list(daily),**_summary(daily),
        'by_year':[{'year':year,**_summary(ds)} for year,ds in sorted(yearly.items())],
        'by_month':[{'month':month,**_summary(ds)} for month,ds in sorted(monthly.items())],
        'quantile_groups':quantile_groups,'quantile_return_horizon_sessions':20,
        'quantile_grouping':'AVERAGE_TIE_FULL_UNIVERSE_MODEL_RANK_PERCENTILE_BINS',
        'quantile_summary_scope':'COMPLETE_DAY_SENSITIVITY_ONLY','quantile_returns':quantile_means,
        'date_equal_top_minus_bottom_forward_return':statistics.fmean(spreads) if spreads else None,
        'limitations':['Partial IC intervals are conservative outer bounds, not sharp or confidence intervals.',
            'Unknown returns and their ranks are never filled or midpoint-imputed.',
            'Nonrandom missing supervision remains a limitation; bounds do not certify economic endpoints.',
            'Overlapping twenty-session outcomes are dependent; no IID significance or executable P&L claim.']}


def select_training_window(reports, *, tolerance=1e-12):
    order=('rolling_2y','rolling_5y','expanding')
    require(set(reports)==set(order) and tolerance==1e-12,'WINDOW_SELECTION_PROTOCOL_MISMATCH')
    signature=None
    for name in order:
        report=reports[name]
        require(report['selection_eligible'] and report['mean_rank_ic_lower_bound'] is not None,
                'UNDEFINED_PRIMARY_RANK_IC_PREVENTS_SELECTION')
        current=tuple((d['as_of'],d['count'],d['label_set_id'],d['knowledge_cutoff']) for d in report['daily'])
        require(signature is None or current==signature,'WINDOW_COMPARISON_INFORMATION_SET_MISMATCH')
        require(report['bound_method']==METHOD and report['target_id']==TARGET_ID,'WINDOW_SELECTION_PROTOCOL_MISMATCH')
        signature=current
    best=max(reports[name]['mean_rank_ic_lower_bound'] for name in order)
    winner=next(name for name in order if best-reports[name]['mean_rank_ic_lower_bound']<=tolerance)
    return {'selected_window':winner,'criterion':'DATE_EQUAL_MEAN_RANK_IC_LOWER_BOUND','tie_tolerance':tolerance,
        'tie_order':list(order),'complete_day_used_for_selection':False,
        'identified_dominance_over_others':all(reports[winner]['mean_rank_ic_lower_bound'] >
            reports[name]['mean_rank_ic_upper_bound'] for name in order if name!=winner),
        'performance_positive':reports[winner]['mean_rank_ic_lower_bound']>0}
