"""Predeclared date-axis HAC and paired outer-bound sensitivity diagnostics."""
from datetime import datetime
import math
import statistics

from quant.contracts import finite, require, session_of


def date_axis_hac(values, calendar, *, lag):
    require(lag in (20,60) and len(calendar)==len(set(calendar))
            and list(calendar)==sorted(calendar) and set(values)<=set(calendar), 'HAC_CALENDAR_INVALID')
    require(bool(values) and all(finite(v) for v in values.values()), 'HAC_VALUES_INVALID')
    mean=statistics.fmean(values.values())
    # A missing calendar position has no observation. Its zero is solely the
    # covariance-product mask; it is never counted as a zero IC value.
    centered=[values[d]-mean if d in values else 0.0 for d in calendar]
    variance=math.fsum(v*v for v in centered)
    for distance in range(1,min(lag,len(calendar)-1)+1):
        covariance=math.fsum(centered[i]*centered[i-distance] for i in range(distance,len(calendar)))
        variance+=2*(1-distance/(lag+1))*covariance
    variance=max(variance,0.0)/len(values)**2
    se=math.sqrt(variance)
    return {'mean':mean,'observed_dates':len(values),'lag':lag,'standard_error':se,
            'normal_95_interval':[mean-1.96*se,mean+1.96*se],
            'role':'SECONDARY_APPROXIMATION_NOT_IDENTIFICATION_BOUNDS_OR_IID_PROOF'}


def bound_sensitivity(daily, calendar):
    require(all(d['rank_ic_lower_bound'] is not None for d in daily), 'UNDEFINED_PRIMARY_RANK_IC')
    lower={session_of(datetime.fromisoformat(d['as_of'])):d['rank_ic_lower_bound'] for d in daily}
    upper={session_of(datetime.fromisoformat(d['as_of'])):d['rank_ic_upper_bound'] for d in daily}
    require(len(lower)==len(daily),'DUPLICATE_EVALUATION_DATE')
    return {str(lag):{'lower_endpoint':date_axis_hac(lower,calendar,lag=lag),
                     'upper_endpoint':date_axis_hac(upper,calendar,lag=lag)} for lag in (20,60)}


def paired_rank_ic_bounds(first,second):
    require(len(first)==len(second),'PAIRED_BOUND_DATES_MISMATCH')
    result=[]
    for a,b in zip(first,second):
        require((a['as_of'],a['label_set_id'],a['count'])==(b['as_of'],b['label_set_id'],b['count']),
                'PAIRED_BOUND_INFORMATION_SET_MISMATCH')
        require(a['rank_ic_lower_bound'] is not None and b['rank_ic_lower_bound'] is not None,
                'UNDEFINED_PRIMARY_RANK_IC')
        result.append({'as_of':a['as_of'],
            'lower':max(-2.0,math.nextafter(a['rank_ic_lower_bound']-b['rank_ic_upper_bound'],-math.inf)),
            'upper':min(2.0,math.nextafter(a['rank_ic_upper_bound']-b['rank_ic_lower_bound'],math.inf))})
    return result
