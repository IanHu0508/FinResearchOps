"""Versioned research definitions shared by preparation, models and readers."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import hashlib
import json
import math
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
TARGET_ID = "cn-a-holding-return-20d-rank-interval/v2"
FEATURE_DEFINITION_ID = "cn-a-daily-price-volume-market-context/v1"
SCALAR_NAMES = (
    "return_1", "return_5", "return_20", "return_60", "overnight_gap",
    "intraday_return", "volatility_5", "volatility_20", "volatility_60",
    "downside_deviation_20", "range_to_close", "volume_ratio_20",
    "amount_ratio_20", "volume_zscore_20", "amount_zscore_20",
    "illiquidity_20", "liquidity_observation_fraction_20", "log_amount",
)
RELATIVE_NAMES = ("relative_return_1", "relative_return_20")
MARKET_NAMES = ("median_return", "breadth", "cross_sectional_dispersion", "market_volatility_20")
SEQUENCE_NAMES = ("total_return_1d", "overnight_gap", "intraday_return",
                  "range_to_close", "log_volume", "log_amount", "tradable")
ABLATIONS = ("stock-only", "stock+context")


class ContractError(ValueError):
    """A violated research contract; never silently repaired or filtered."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identifier(value):
    return isinstance(value, str) and bool(value.strip())


def aware(value):
    require(isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None, "TIMEZONE_REQUIRED")
    return value


def session_of(value):
    return aware(value).astimezone(CHINA).date()


def market_time(session, hour=18, minute=0):
    return datetime.combine(session, time(hour, minute), CHINA)


def evaluation_cutoff(phase):
    require(phase in ('development','final'), 'UNKNOWN_EVALUATION_PHASE')
    return market_time(date(2024,1,1) if phase=='development' else date(2026,4,1),0)


def primitive(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return primitive(asdict(value))
    if isinstance(value, dict):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    return value


def canonical(value):
    return json.dumps(primitive(value), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class ResearchSpec:
    universe_id: str
    lookback_sessions: int = 60
    horizon_sessions: int = 20
    target_id: str = TARGET_ID
    feature_definition_id: str = FEATURE_DEFINITION_ID

    def __post_init__(self):
        require(identifier(self.universe_id), "UNIVERSE_ID_REQUIRED")
        require(self.horizon_sessions == 20 and type(self.horizon_sessions) is int,
                "V1_HORIZON_IS_20_TRADING_SESSIONS")
        require(self.lookback_sessions == 60 and type(self.lookback_sessions) is int,
                "V1_LOOKBACK_IS_60_TRADING_SESSIONS")
        require(self.target_id == TARGET_ID, "TARGET_DEFINITION_MISMATCH")
        require(self.feature_definition_id == FEATURE_DEFINITION_ID, "FEATURE_DEFINITION_MISMATCH")


@dataclass(frozen=True, order=True)
class SampleKey:
    symbol: str
    as_of: datetime

    def __post_init__(self):
        require(identifier(self.symbol), "SYMBOL_REQUIRED")
        aware(self.as_of)


@dataclass(frozen=True)
class FeatureRow:
    key: SampleKey
    scalars: tuple[float | None, ...]
    sequence: tuple[tuple[float, ...], ...]
    relative_features: tuple[float, ...]
    market_context: tuple[float, ...]
    market_context_id: str
    data_cutoff: datetime
    source_ids: tuple[str, ...]

    def __post_init__(self):
        require(type(self.scalars) is tuple and type(self.sequence) is tuple
                and type(self.relative_features) is tuple and type(self.market_context) is tuple
                and all(type(x) is tuple for x in self.sequence)
                and type(self.source_ids) is tuple, "FEATURE_ROW_MUST_BE_IMMUTABLE")
        require(len(self.scalars) == len(SCALAR_NAMES), "SCALAR_SHAPE_INVALID")
        require(all(v is None or finite(v) for v in self.scalars), "NONFINITE_FEATURE")
        require(len(self.sequence) == 60 and all(len(x) == len(SEQUENCE_NAMES)
                and all(finite(v) for v in x) for x in self.sequence), "SEQUENCE_SHAPE_INVALID")
        require(len(self.relative_features) == len(RELATIVE_NAMES)
                and all(finite(v) for v in self.relative_features), "RELATIVE_FEATURE_SHAPE_INVALID")
        require(len(self.market_context) == len(MARKET_NAMES)
                and all(finite(v) for v in self.market_context), "MARKET_CONTEXT_SHAPE_INVALID")
        require(0 <= self.market_context[1] <= 1 and all(v >= 0 for v in self.market_context[2:]),
                "MARKET_CONTEXT_DOMAIN_INVALID")
        require(isinstance(self.market_context_id, str) and len(self.market_context_id) == 64
                and all(c in "0123456789abcdef" for c in self.market_context_id), "MARKET_CONTEXT_ID_INVALID")
        require(aware(self.data_cutoff) <= self.key.as_of, "FUTURE_FEATURE_INPUT")
        require(bool(self.source_ids)
                and all(identifier(x) for x in self.source_ids), "FEATURE_PROVENANCE_REQUIRED")


@dataclass(frozen=True)
class Panel:
    spec: ResearchSpec
    rows: tuple[FeatureRow, ...]
    data_kind: str

    def __post_init__(self):
        require(type(self.rows) is tuple and all(isinstance(r, FeatureRow) for r in self.rows),
                "PANEL_ROWS_MUST_BE_IMMUTABLE")
        require(self.data_kind in ("SYNTHETIC", "REAL_DATA"), "DATA_KIND_INVALID")
        require(bool(self.rows), "EMPTY_PANEL")
        require(len({r.key for r in self.rows}) == len(self.rows), "DUPLICATE_SAMPLE")
        require(len({(r.key.symbol, session_of(r.key.as_of)) for r in self.rows}) == len(self.rows),
                "MULTIPLE_SNAPSHOTS_FOR_STOCK_DATE")
        cutoffs, contexts = {}, {}
        for row in self.rows:
            cutoffs.setdefault(session_of(row.key.as_of), set()).add(row.key.as_of)
            contexts.setdefault(row.key.as_of, set()).add((row.market_context, row.market_context_id))
        require(all(len(v) == 1 for v in cutoffs.values()), "MIXED_SCORING_CUTOFF_WITHIN_DATE")
        require(all(len(v) == 1 for v in contexts.values()), "MIXED_MARKET_CONTEXT_WITHIN_DATE")
        require(tuple(sorted(self.rows, key=lambda r: r.key)) == self.rows, "PANEL_ORDER_INVALID")

    @property
    def dataset_id(self):
        return fingerprint(self)


@dataclass(frozen=True)
class LabelRow:
    key: SampleKey
    entry_date: date | None
    label_end_date: date | None
    available_at: datetime | None
    raw_return: float | None
    target_interval: tuple[float, float] | None
    universe_size: int
    observed_count: int
    knowledge_cutoff: datetime
    outcome_available_at: datetime | None
    missing_reason: str | None = None

    def __post_init__(self):
        require(self.raw_return is None or finite(self.raw_return), "NONFINITE_LABEL")
        require(type(self.universe_size) is int and self.universe_size >= 2
                and type(self.observed_count) is int and 0 <= self.observed_count <= self.universe_size,
                "LABEL_POOL_COUNTS_INVALID")
        aware(self.knowledge_cutoff)
        if self.available_at is not None:
            aware(self.available_at)
        if self.target_interval is not None:
            require(type(self.target_interval) is tuple and len(self.target_interval) == 2
                    and all(finite(x) for x in self.target_interval)
                    and 0 <= self.target_interval[0] <= self.target_interval[1] <= 1
                    and self.missing_reason is None, "LABEL_INTERVAL_INVALID")
            require(self.entry_date is not None and self.label_end_date is not None
                    and session_of(self.key.as_of) < self.entry_date <= self.label_end_date,
                    "LABEL_WINDOW_INVALID")
            require(self.available_at is not None
                    and self.outcome_available_at is not None
                    and market_time(self.label_end_date, 15) <= aware(self.outcome_available_at)
                    <= self.available_at < self.knowledge_cutoff
                    and self.label_end_date < session_of(self.knowledge_cutoff),
                    "LABEL_AVAILABILITY_INVALID")
            require(self.raw_return is not None, "LABEL_RETURN_MISSING")
        else:
            require(self.raw_return is None and self.outcome_available_at is None
                    and identifier(self.missing_reason), "UNKNOWN_OUTCOME_MUST_NOT_HAVE_POINT_VALUE")

    @property
    def supervised(self):
        return self.target_interval is not None

    @property
    def target_percentile(self):
        """A point exists only for an identified rank, never an interval midpoint."""
        if self.target_interval is not None and self.target_interval[0] == self.target_interval[1]:
            return self.target_interval[0]
        return None

    @property
    def complete(self):
        return self.supervised and self.observed_count == self.universe_size


@dataclass(frozen=True)
class LabeledDataset:
    panel: Panel
    labels: tuple[LabelRow, ...]

    def __post_init__(self):
        require(type(self.labels) is tuple and all(isinstance(y, LabelRow) for y in self.labels),
                "LABELS_MUST_BE_IMMUTABLE")
        require(tuple(r.key for r in self.panel.rows) == tuple(r.key for r in self.labels),
                "LABEL_SAMPLE_ALIGNMENT_INVALID")
        require(all(y.supervised or y.missing_reason is not None for y in self.labels),
                "UNEXPLAINED_MISSING_LABEL")
        validate_label_groups(self.labels)

    @property
    def dataset_id(self):
        return fingerprint(self)


def validate_label_groups(rows):
    require(len({y.key for y in rows})==len(rows),'DUPLICATE_LABEL_SAMPLE')
    groups = {}
    for label in rows:
        groups.setdefault(label.key.as_of, []).append(label)
    from quant.labels.intervals import percentile_intervals
    for labels in groups.values():
        observed = sum(y.supervised for y in labels)
        require(all(y.universe_size == len(labels) and y.observed_count == observed for y in labels),
                "LABEL_FULL_UNIVERSE_ALIGNMENT_INVALID")
        require(len({(y.entry_date, y.label_end_date, y.knowledge_cutoff, y.available_at)
                     for y in labels}) == 1, "MIXED_LABEL_INFORMATION_SET")
        expected = percentile_intervals(tuple(y.raw_return for y in labels))
        require(tuple(y.target_interval for y in labels) == expected, "LABEL_INTERVAL_RANK_MISMATCH")
        require(labels[0].available_at == max((y.outcome_available_at for y in labels
                if y.supervised), default=None), "LABEL_DEPENDENCY_AVAILABILITY_MISMATCH")


@dataclass(frozen=True)
class Prediction:
    key: SampleKey
    predicted_target_percentile: float

    def __post_init__(self):
        require(finite(self.predicted_target_percentile)
                and 0 <= self.predicted_target_percentile <= 1, "PREDICTION_OUTSIDE_PERCENTILE_RANGE")
