from collections import Counter
from dataclasses import dataclass
from datetime import date

from quant.contracts import FeatureRow, LabelRow, SampleKey, finite, market_time, require, session_of


@dataclass(frozen=True)
class FoldWindow:
    train_start: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date

    def __post_init__(self):
        require(all(type(d) is date for d in (self.train_start, self.validation_start,
                self.validation_end, self.test_start, self.test_end)), "FOLD_DATE_REQUIRED")
        require(self.train_start < self.validation_start <= self.validation_end
                < self.test_start <= self.test_end, "FOLD_ORDER_INVALID")


@dataclass(frozen=True)
class TrainingBatch:
    rows: tuple[FeatureRow, ...]
    targets: tuple[float, ...]
    weights: tuple[float, ...]
    label_available_at: tuple
    validation_start: date
    dataset_id: str

    def __post_init__(self):
        require(all(type(value) is tuple for value in (self.rows, self.targets, self.weights,
                    self.label_available_at)), "TRAINING_BATCH_MUST_BE_IMMUTABLE")
        require(bool(self.rows) and len(self.rows) == len(self.targets) == len(self.weights)
                == len(self.label_available_at), "TRAINING_SHAPE_INVALID")
        require(len({r.key for r in self.rows}) == len(self.rows), "DUPLICATE_TRAINING_SAMPLE")
        require(all(finite(q) and 0 <= q <= 1 for q in self.targets), "TRAINING_TARGET_INVALID")
        require(all(finite(w) and w > 0 for w in self.weights), "TRAINING_WEIGHT_INVALID")
        cutoff = market_time(self.validation_start, 0)
        require(all(r.key.as_of < cutoff and ready < cutoff
                    for r, ready in zip(self.rows, self.label_available_at)), "TRAINING_LABEL_LEAKAGE")
        counts = Counter(r.key.as_of for r in self.rows)
        require(all(abs(w - 1 / counts[r.key.as_of]) < 1e-12 for r, w in zip(self.rows, self.weights)),
                "DATE_WEIGHTS_INVALID")


@dataclass(frozen=True)
class EvaluationBatch:
    rows: tuple[FeatureRow, ...]
    labels: tuple[LabelRow, ...]

    def __post_init__(self):
        require(bool(self.rows) and tuple(r.key for r in self.rows) == tuple(y.key for y in self.labels),
                "EVALUATION_ALIGNMENT_INVALID")
        require(all(y.complete for y in self.labels), "EVALUATION_OUTCOME_INCOMPLETE")


@dataclass(frozen=True)
class PreparedFold:
    window: FoldWindow
    train: TrainingBatch
    validation: EvaluationBatch
    test: EvaluationBatch
    purged_train: tuple[SampleKey, ...]
    purged_validation: tuple[SampleKey, ...]
    unavailable_labels: tuple[SampleKey, ...]


def partition_label(label, window):
    """The same date and label-end decision for in-memory and disk experiments."""
    day = session_of(label.key.as_of)
    kind = ("train" if window.train_start <= day < window.validation_start else
            "validation" if window.validation_start <= day <= window.validation_end else
            "test" if window.test_start <= day <= window.test_end else None)
    if kind is None:
        return None, "OUTSIDE_WINDOW"
    if not label.complete:
        return kind, "OUTCOME_UNAVAILABLE"
    boundary = window.validation_start if kind == "train" else window.test_start
    if kind != "test" and (label.label_end_date >= boundary
                           or label.available_at >= market_time(boundary, 0)):
        return kind, "PURGED"
    return kind, "AVAILABLE"


def prepare_fold(dataset, window):
    selected = {name: [] for name in ("train", "validation", "test")}
    purged_train, purged_validation, unavailable = [], [], []
    for row, label in zip(dataset.panel.rows, dataset.labels):
        kind, disposition = partition_label(label, window)
        if kind is None:
            continue
        if disposition == "OUTCOME_UNAVAILABLE":
            unavailable.append(row.key)
            continue
        if disposition == "PURGED":
            (purged_train if kind == "train" else purged_validation).append(row.key)
            continue
        selected[kind].append((row, label))
    require(all(selected.values()), "EMPTY_SPLIT_AFTER_PURGE")
    counts = Counter(r.key.as_of for r, _ in selected["train"])
    train = TrainingBatch(tuple(r for r, _ in selected["train"]),
        tuple(y.target_percentile for _, y in selected["train"]),
        tuple(1 / counts[r.key.as_of] for r, _ in selected["train"]),
        tuple(y.available_at for _, y in selected["train"]), window.validation_start, dataset.dataset_id)
    def evaluation(name):
        return EvaluationBatch(tuple(r for r, _ in selected[name]), tuple(y for _, y in selected[name]))
    return PreparedFold(window, train, evaluation("validation"), evaluation("test"),
                        tuple(purged_train), tuple(purged_validation), tuple(unavailable))
