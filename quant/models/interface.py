"""Models consume shared prepared rows; predict receives no labels."""

from typing import Protocol

from quant.contracts import FeatureRow, Prediction
from quant.splits.walk_forward import TrainingBatch


class FittedModel(Protocol):
    @property
    def ablation(self) -> str: ...

    @property
    def view(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    @property
    def training_cutoff(self): ...

    @property
    def artifact(self) -> dict: ...

    def predict(self, rows: tuple[FeatureRow, ...]) -> tuple[Prediction, ...]: ...


class Model(Protocol):
    def fit(self, train: TrainingBatch) -> FittedModel: ...
