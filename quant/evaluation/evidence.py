"""Validation evidence recomputed from persisted forecasts with only the outcomes known at a cutoff.

Forecasts are never refitted. Labels are censored at the cutoff and re-ranked on the
complete pool before any metric; unknown outcomes are neither filled nor midpointed.
A research note may cite the result only as knowledge available at that cutoff.
"""

from dataclasses import dataclass
from datetime import datetime
import statistics

from quant.contracts import aware, finite, identifier, require
from quant.labels.forward import label_rows_at_cutoff
from quant.splits.walk_forward import EvaluationBatch
from .prediction import evaluate_predictions, summarize_daily

SCOPES = ("EXACT_MODEL", "METHOD_FAMILY")


@dataclass(frozen=True)
class ValidationEvidence:
    """Model-level history known at `knowledge_cutoff`; never a single security's win rate.

    `complete_days` counts complete-outcome days whose top and bottom groups are
    both non-empty; the two group differences are date-equal means over them.
    """

    scope: str
    model_version: str
    label: str
    knowledge_cutoff: datetime
    forecast_days: int
    evaluable_days: int
    period: tuple[str, str] | None
    rank_ic_bounds: tuple[float, float] | None
    negative_ic_months: tuple[str, ...]
    complete_days: int
    top_group_minus_pool: float | None
    bottom_group_minus_pool: float | None

    def __post_init__(self):
        require(self.scope in SCOPES, "EVIDENCE_SCOPE_INVALID")
        require(identifier(self.model_version) and identifier(self.label), "EVIDENCE_IDENTITY_REQUIRED")
        aware(self.knowledge_cutoff)
        require(all(type(v) is int for v in (self.forecast_days, self.evaluable_days, self.complete_days))
                and 0 <= self.complete_days <= self.evaluable_days <= self.forecast_days, "EVIDENCE_COUNTS_INVALID")
        require((self.period is None) == (self.evaluable_days == 0), "EVIDENCE_PERIOD_INVALID")
        require(self.rank_ic_bounds is None or (type(self.rank_ic_bounds) is tuple and len(self.rank_ic_bounds) == 2
                and all(finite(v) for v in self.rank_ic_bounds) and self.rank_ic_bounds[0] <= self.rank_ic_bounds[1]),
                "EVIDENCE_RANK_IC_BOUNDS_INVALID")
        require(type(self.negative_ic_months) is tuple, "EVIDENCE_MONTHS_INVALID")
        groups = (self.top_group_minus_pool, self.bottom_group_minus_pool)
        require(all(finite(v) for v in groups) if self.complete_days else groups == (None, None),
                "EVIDENCE_GROUP_RETURNS_INVALID")
        require(self.evaluable_days or (self.rank_ic_bounds is None and not self.negative_ic_months),
                "EVIDENCE_WITHOUT_EVALUABLE_DAYS")


def validation_evidence(days, *, cutoff, scope, model_version, label, quantile_groups=5):
    """Summarize persisted forecasts whose outcomes are known at `cutoff`.

    `days` yields (predictions, labels) for complete scoring pools. Every forecast
    must exist by the cutoff; a day whose horizon has not matured by then is
    counted but not evaluated. The evaluator reads only keys from its rows, so the
    label rows serve as the key view without any feature or price being rebuilt.
    """
    cutoff = aware(cutoff)
    daily, forecast_days = [], 0
    for predictions, labels in days:
        labels = label_rows_at_cutoff(tuple(labels), cutoff)
        require(labels[0].key.as_of <= cutoff, "FORECAST_AFTER_EVIDENCE_CUTOFF")
        forecast_days += 1
        if any(y.supervised for y in labels):
            daily.extend(evaluate_predictions(tuple(predictions), EvaluationBatch(labels, labels),
                                              quantile_groups=quantile_groups)["daily"])
    if not daily:
        return ValidationEvidence(scope, model_version, label, cutoff, forecast_days, 0, None, None, (), 0, None, None)
    daily.sort(key=lambda day: day["as_of"])
    summary = summarize_daily(daily, quantile_groups=quantile_groups)
    lower, upper = summary["mean_rank_ic_lower_bound"], summary["mean_rank_ic_upper_bound"]
    negative = tuple(month["month"] for month in summary["by_month"]
                     if month["mean_rank_ic_upper_bound"] is not None and month["mean_rank_ic_upper_bound"] < 0)
    top, bottom = [], []
    for day in daily:
        groups = day["quantiles"]
        if day["unknown_count"] or groups[0]["mean_forward_return"] is None or groups[-1]["mean_forward_return"] is None:
            continue
        pool = sum(g["mean_forward_return"] * g["count"] for g in groups if g["count"]) / day["count"]
        top.append(groups[-1]["mean_forward_return"] - pool)
        bottom.append(groups[0]["mean_forward_return"] - pool)
    return ValidationEvidence(
        scope, model_version, label, cutoff, forecast_days, len(daily), (daily[0]["as_of"][:10], daily[-1]["as_of"][:10]),
        None if lower is None or upper is None else (lower, upper), negative, len(top),
        statistics.fmean(top) if top else None, statistics.fmean(bottom) if bottom else None)
