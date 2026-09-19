"""Declared model views, train-only imputation and train-only scaling."""

from dataclasses import dataclass
import math

from quant.contracts import ABLATIONS, MARKET_NAMES, RELATIVE_NAMES, SCALAR_NAMES, SEQUENCE_NAMES, finite, require

VIEWS = ("tabular", "linear", "sequence", "hybrid")


def vector_names(view, ablation="stock-only"):
    require(view in VIEWS, "UNKNOWN_MODEL_VIEW")
    require(ablation in ABLATIONS, "UNKNOWN_FEATURE_ABLATION")
    names = () if view == "sequence" else SCALAR_NAMES
    if ablation == "stock+context":
        names += RELATIVE_NAMES
        if view == "linear":
            names += tuple(f"interaction:{stock}*{market}" for stock in SCALAR_NAMES for market in MARKET_NAMES)
        else:
            names += MARKET_NAMES
    names += tuple("missing:" + name for name in names)
    if view in ("sequence", "hybrid"):
        names += tuple(f"sequence[{offset - 59}]:{name}" for offset in range(60) for name in SEQUENCE_NAMES)
    return names


def vectors(rows, view, ablation="stock-only"):
    names = vector_names(view, ablation)
    result = []
    for row in rows:
        scalar = () if view == "sequence" else row.scalars
        if ablation == "stock+context":
            scalar += row.relative_features
            if view == "linear":
                # A market-only additive term cannot alter same-day stock order.
                scalar += tuple(None if stock is None else stock * market
                                for stock in row.scalars for market in row.market_context)
            else:
                scalar += row.market_context
        values = tuple(scalar) + tuple(float(v is None) for v in scalar)
        if view in ("sequence", "hybrid"):
            values += tuple(v for session in row.sequence for v in session)
        require(len(values) == len(names), "MODEL_VIEW_SHAPE_INVALID")
        result.append(values)
    return tuple(result)


@dataclass(frozen=True)
class TrainingTransform:
    means: tuple[float, ...]
    scales: tuple[float, ...]

    @classmethod
    def fit(cls, values, weights):
        require(bool(values) and len(values) == len(weights), "TRANSFORM_TRAINING_EMPTY")
        require(len({len(row) for row in values}) == 1, "TRANSFORM_SHAPE_INVALID")
        means, scales = [], []
        for column in zip(*values):
            observed = [(v, w) for v, w in zip(column, weights) if v is not None]
            total = sum(w for _, w in observed)
            mean = sum(v * w for v, w in observed) / total if total else 0.0
            variance = sum(w * (v - mean) ** 2 for v, w in observed) / total if total else 0.0
            means.append(mean)
            scales.append(math.sqrt(variance) if variance > 1e-24 else 1.0)
        return cls(tuple(means), tuple(scales))

    def apply(self, values):
        require(all(len(row) == len(self.means) for row in values), "TRANSFORM_SHAPE_INVALID")
        result = tuple(tuple((mean if v is None else v) - mean for v, mean in zip(row, self.means))
                       for row in values)
        result = tuple(tuple(v / scale for v, scale in zip(row, self.scales)) for row in result)
        require(all(finite(v) for row in result for v in row), "TRANSFORM_NONFINITE")
        return result
