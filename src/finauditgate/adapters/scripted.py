"""A deterministic candidate Adapter for tests and offline development."""

from types import MappingProxyType
from typing import Mapping

from finauditgate.contracts import AuditTask
from finauditgate.ports.model import (
    CalculationCandidate,
    EvidenceCandidate,
    ModelCandidate,
)


ScriptedCandidate = ModelCandidate


class ScriptedModelAdapter:
    """Return predeclared candidates without network or model inference."""

    def __init__(self, candidates: Mapping[str, ModelCandidate]) -> None:
        self._candidates = MappingProxyType(dict(candidates))

    def propose(self, task: AuditTask) -> ModelCandidate:
        try:
            return self._candidates[task.task_id]
        except KeyError as exc:
            raise LookupError(
                f"no scripted candidate for task {task.task_id!r}"
            ) from exc
