from collections import Counter
from dataclasses import dataclass
from datetime import date

from quant.contracts import FeatureRow, LabelRow, SampleKey, aware, evaluation_cutoff, finite, market_time, require, session_of, validate_label_groups
from quant.labels.forward import labels_at_cutoff


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
    targets: tuple[tuple[float, float], ...]
    weights: tuple[float, ...]
    label_available_at: tuple
    validation_start: date
    dataset_id: str
    label_knowledge_cutoffs: tuple

    def __post_init__(self):
        require(all(type(value) is tuple for value in (self.rows, self.targets, self.weights,
                    self.label_available_at, self.label_knowledge_cutoffs)), "TRAINING_BATCH_MUST_BE_IMMUTABLE")
        require(bool(self.rows) and len(self.rows) == len(self.targets) == len(self.weights)
                == len(self.label_available_at) == len(self.label_knowledge_cutoffs), "TRAINING_SHAPE_INVALID")
        require(len({r.key for r in self.rows}) == len(self.rows), "DUPLICATE_TRAINING_SAMPLE")
        require(all(type(q) is tuple and len(q) == 2 and all(finite(v) for v in q)
                    and 0 <= q[0] <= q[1] <= 1 for q in self.targets), "TRAINING_TARGET_INVALID")
        require(all(finite(w) and w > 0 for w in self.weights), "TRAINING_WEIGHT_INVALID")
        cutoff = market_time(self.validation_start, 0)
        require(all(r.key.as_of < cutoff and aware(ready) < cutoff
                    for r, ready in zip(self.rows, self.label_available_at)), "TRAINING_LABEL_LEAKAGE")
        require(all(aware(c) == cutoff for c in self.label_knowledge_cutoffs), "TRAINING_RANK_CUTOFF_MISMATCH")
        counts = Counter(r.key.as_of for r in self.rows)
        require(all(abs(w - 1 / counts[r.key.as_of]) < 1e-12 for r, w in zip(self.rows, self.weights)),
                "DATE_WEIGHTS_INVALID")


@dataclass(frozen=True)
class EvaluationBatch:
    rows: tuple[FeatureRow, ...]
    labels: tuple[LabelRow, ...]

    def __post_init__(self):
        require(type(self.rows) is tuple and type(self.labels) is tuple and bool(self.rows)
                and len({r.key for r in self.rows}) == len(self.rows)
                and tuple(r.key for r in self.rows) == tuple(y.key for y in self.labels),
                "EVALUATION_ALIGNMENT_INVALID")
        validate_label_groups(self.labels)


@dataclass(frozen=True)
class PreparedFold:
    window: FoldWindow
    train: TrainingBatch
    validation: EvaluationBatch
    test: EvaluationBatch
    purged_train: tuple[SampleKey, ...]
    purged_validation: tuple[SampleKey, ...]
    purged_test: tuple[SampleKey, ...]
    unavailable_labels: tuple[SampleKey, ...]


def split_kind(day, window):
    return ("train" if window.train_start <= day < window.validation_start else
            "validation" if window.validation_start <= day <= window.validation_end else
            "test" if window.test_start <= day <= window.test_end else None)


def partition_label(label, window):
    kind = split_kind(session_of(label.key.as_of), window)
    if kind is None:
        return None, "OUTSIDE_WINDOW"
    boundary = window.validation_start if kind == "train" else window.test_start
    if label.label_end_date is None:
        return kind, "OUTCOME_UNAVAILABLE"
    if label.label_end_date >= session_of(label.knowledge_cutoff):
        return kind, "PURGED"
    if kind != "test":
        if label.label_end_date >= boundary:
            return kind, "PURGED"
        require(label.knowledge_cutoff <= market_time(boundary, 0), "REIDENTIFY_RANKS_BEFORE_SPLIT")
    if not label.supervised:
        return kind, "OUTCOME_UNAVAILABLE"
    return kind, "AVAILABLE"


def day_partition(labels, window):
    require(bool(labels) and len({y.key.as_of for y in labels})==1,"DATE_PARTITION_INPUT_INVALID")
    validate_label_groups(labels)
    first=labels[0]
    kind=split_kind(session_of(first.key.as_of),window)
    if kind is None:
        return None,'OUTSIDE_WINDOW'
    if first.label_end_date is None:
        return kind,'OUTCOME_UNAVAILABLE'
    if kind!='test':
        boundary=window.validation_start if kind=='train' else window.test_start
        if first.label_end_date>=boundary:
            return kind,'PURGED'
        require(first.knowledge_cutoff<=market_time(boundary,0),'REIDENTIFY_RANKS_BEFORE_SPLIT')
    if first.label_end_date>=session_of(first.knowledge_cutoff):
        return kind,'PURGED'
    # Partial/unknown observations do not remove mature dates from evaluation.
    return kind,'AVAILABLE'


def training_batch(dataset, fit_date, *, train_start=None):
    cutoff = market_time(fit_date, 0)
    require(all(y.knowledge_cutoff == cutoff for y in dataset.labels), "TRAINING_RANK_CUTOFF_MISMATCH")
    selected = [(r,y) for r,y in zip(dataset.panel.rows,dataset.labels)
                if (train_start is None or session_of(r.key.as_of) >= train_start)
                and r.key.as_of < cutoff and y.label_end_date is not None
                and y.label_end_date < fit_date and y.supervised]
    require(bool(selected), "EMPTY_TRAINING_AFTER_PURGE")
    counts = Counter(r.key.as_of for r,_ in selected)
    return TrainingBatch(tuple(r for r,_ in selected), tuple(y.target_interval for _,y in selected),
        tuple(1/counts[r.key.as_of] for r,_ in selected), tuple(y.available_at for _,y in selected),
        fit_date,dataset.dataset_id,tuple(y.knowledge_cutoff for _,y in selected))


def prepare_fold(dataset, window, *, evaluation_knowledge_cutoff=None):
    # Re-identification precedes row selection: late/unknown members remain in
    # each full historical pool, although their supervised row is not emitted.
    require(dataset.panel.data_kind=='SYNTHETIC' or evaluation_knowledge_cutoff is not None,
            'REAL_EVALUATION_CUTOFF_REQUIRED')
    if dataset.panel.data_kind!='SYNTHETIC':
        require(evaluation_knowledge_cutoff in (evaluation_cutoff('development'),evaluation_cutoff('final')),
                'EVALUATION_CUTOFF_PROTOCOL_MISMATCH')
    test_view=(labels_at_cutoff(dataset,evaluation_knowledge_cutoff)
               if evaluation_knowledge_cutoff is not None else dataset)
    test_cap=min(y.knowledge_cutoff for y in test_view.labels)
    train_view = labels_at_cutoff(dataset, market_time(window.validation_start,0))
    validation_view = labels_at_cutoff(dataset, min(market_time(window.test_start,0),test_cap))
    views = {"train":train_view,"validation":validation_view,"test":test_view}
    selected = {name:[] for name in ("train","validation","test")}
    purged={'train':[],'validation':[],'test':[]}; unavailable=[]
    lookups = {name:{y.key:y for y in view.labels} for name,view in views.items()}
    for row in dataset.panel.rows:
        kind = split_kind(session_of(row.key.as_of),window)
        if kind is None:
            continue
        label = lookups[kind][row.key]
        require(label.label_end_date is not None,'HORIZON_COVERAGE_INCOMPLETE')
        _, disposition = partition_label(label, window)
        if disposition == "PURGED":
            purged[kind].append(row.key)
            continue
        if disposition == "OUTCOME_UNAVAILABLE":
            unavailable.append(row.key)
        if kind != "train":
            # Evaluation/inference keeps unknown members and never survivor-ranks.
            selected[kind].append((row,label))
    require(selected['validation'] and selected['test'], "EMPTY_SPLIT_AFTER_PURGE")
    train = training_batch(train_view,window.validation_start,train_start=window.train_start)
    def evaluation(name):
        return EvaluationBatch(tuple(r for r,_ in selected[name]),tuple(y for _,y in selected[name]))
    return PreparedFold(window,train,evaluation('validation'),evaluation('test'),
                        tuple(purged['train']),tuple(purged['validation']),tuple(purged['test']),tuple(unavailable))
