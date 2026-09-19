"""Dependency-free supervised reference adapters for plumbing checks.

These are not an implementation or performance substitute for XGBoost/DL.
"""

from dataclasses import dataclass
from datetime import datetime

from quant.contracts import ABLATIONS, Prediction, fingerprint, primitive, require
from quant.models.views import VIEWS, TrainingTransform, vectors


@dataclass(frozen=True)
class FittedMean:
    value: float
    training_cutoff: datetime
    training_dataset_id: str
    ablation: str

    def __post_init__(self):
        require(self.ablation in ABLATIONS, "UNKNOWN_FEATURE_ABLATION")

    @property
    def view(self):
        return "constant"

    @property
    def model_version(self):
        return "mean-v2-" + fingerprint(self)[:24]

    @property
    def artifact(self):
        return {"schema_version": "quant.reference-model/v2", "kind": "mean",
                "model_version": self.model_version, "state": primitive(self)}

    def predict(self, rows):
        return tuple(Prediction(row.key, self.value) for row in rows)


@dataclass(frozen=True)
class MeanModel:
    ablation: str = "stock+context"

    def fit(self, train):
        return FittedMean(sum(y * w for y, w in zip(train.targets, train.weights)) / sum(train.weights),
                          max(train.label_available_at), train.dataset_id, self.ablation)


@dataclass(frozen=True)
class FittedNeighbors:
    k: int
    view: str
    ablation: str
    transform: TrainingTransform
    training_vectors: tuple[tuple[float, ...], ...]
    targets: tuple[float, ...]
    weights: tuple[float, ...]
    training_cutoff: datetime
    training_dataset_id: str

    def __post_init__(self):
        require(self.view in VIEWS, "UNKNOWN_MODEL_VIEW")
        require(self.ablation in ABLATIONS, "UNKNOWN_FEATURE_ABLATION")

    @property
    def model_version(self):
        return "neighbors-v2-" + fingerprint(self)[:24]

    @property
    def artifact(self):
        return {"schema_version": "quant.reference-model/v2", "kind": "neighbors",
                "model_version": self.model_version, "state": primitive(self)}

    def predict(self, rows):
        transformed = self.transform.apply(vectors(rows, self.view, self.ablation))
        results = []
        for row, values in zip(rows, transformed):
            distances = tuple(sum((a - b) ** 2 for a, b in zip(values, candidate))
                              for candidate in self.training_vectors)
            nearest = sorted(range(len(distances)), key=lambda i: (distances[i], i))[:self.k]
            total = sum(self.weights[i] for i in nearest)
            score = sum(self.weights[i] * self.targets[i] for i in nearest) / total
            results.append(Prediction(row.key, score))
        return tuple(results)


@dataclass(frozen=True)
class NearestNeighborsModel:
    k: int = 5
    view: str = "tabular"
    ablation: str = "stock+context"

    def fit(self, train):
        require(type(self.k) is int and 1 <= self.k <= len(train.rows), "NEIGHBOR_COUNT_INVALID")
        values = vectors(train.rows, self.view, self.ablation)
        transform = TrainingTransform.fit(values, train.weights)
        return FittedNeighbors(self.k, self.view, self.ablation, transform, transform.apply(values),
                               train.targets, train.weights, max(train.label_available_at), train.dataset_id)


def restore_reference_model(document):
    require(document.get("schema_version") == "quant.reference-model/v2", "MODEL_SCHEMA_INVALID")
    state = dict(document["state"])
    state["training_cutoff"] = datetime.fromisoformat(state["training_cutoff"])
    if document.get("kind") == "mean":
        model = FittedMean(**state)
    else:
        require(document.get("kind") == "neighbors", "UNKNOWN_REFERENCE_MODEL")
        state["transform"] = TrainingTransform(tuple(state["transform"]["means"]),
                                                tuple(state["transform"]["scales"]))
        for key in ("targets", "weights"):
            state[key] = tuple(state[key])
        state["training_vectors"] = tuple(tuple(row) for row in state["training_vectors"])
        model = FittedNeighbors(**state)
    require(model.model_version == document.get("model_version"), "MODEL_CONTENT_MISMATCH")
    return model
