"""Label-free scoring of one declared pool from a store bounded at the scoring date."""

from dataclasses import dataclass

from quant.contracts import Panel, Prediction, ResearchSpec, require
from quant.data.market_store import MarketStore
from quant.features import build_panel
from quant.labels.ranks import percentiles


@dataclass(frozen=True)
class ScoredDay:
    """Full-pool model output for one scoring time; it carries no outcome."""

    panel: Panel
    predictions: tuple[Prediction, ...]
    ranks: tuple[float, ...]
    model_version: str

    def __post_init__(self):
        keys = tuple(row.key for row in self.panel.rows)
        require(len({key.as_of for key in keys}) == 1, "SCORED_DAY_MIXED_AS_OF")
        require(type(self.predictions) is tuple and tuple(p.key for p in self.predictions) == keys,
                "SCORED_DAY_COVERAGE_MISMATCH")
        require(self.ranks == percentiles(tuple(p.predicted_target_percentile for p in self.predictions)),
                "SCORED_DAY_RANK_MISMATCH")


def score_day(store_path, day, fitted):
    """Score the complete pool U_t with a frozen fitted model.

    The store is opened with `through=day`, so no later session, bar, pool,
    outcome price or label can be read. Ranks use the same average-tie
    percentile convention as build_signals.
    """
    with MarketStore(store_path, through=day) as store:
        data = store.read_feature_day(day)
        spec = ResearchSpec(store.universe_id)
    panel = build_panel(data, spec)
    as_of = panel.rows[0].key.as_of
    require(fitted.training_cutoff < as_of, "MODEL_INFORMATION_AFTER_SCORING_TIME")
    predictions = tuple(fitted.predict(panel.rows))
    ranks = percentiles(tuple(p.predicted_target_percentile for p in predictions))
    return ScoredDay(panel, predictions, ranks, fitted.model_version)
