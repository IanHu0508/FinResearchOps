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
ScriptedAttemptSequence = tuple[ModelCandidate, ...]


class ScriptedModelAdapter:
    """Return predeclared candidates without network or model inference."""

    def __init__(
        self,
        candidates: Mapping[
            str,
            ModelCandidate | ScriptedAttemptSequence,
        ],
    ) -> None:
        self._candidates = MappingProxyType(dict(candidates))

    def propose(
        self,
        task: AuditTask,
        attempt_index: int = 0,
    ) -> ModelCandidate:
        if type(attempt_index) is not int or attempt_index < 0:
            raise ValueError("attempt_index must be a non-negative integer")
        try:
            candidate = self._candidates[task.task_id]
        except KeyError as exc:
            raise LookupError(
                f"no scripted candidate for task {task.task_id!r}"
            ) from exc
        if type(candidate) is tuple:
            try:
                return candidate[attempt_index]
            except IndexError as exc:
                raise LookupError(
                    f"no scripted attempt {attempt_index} for task "
                    f"{task.task_id!r}"
                ) from exc
        if attempt_index != 0:
            raise LookupError(
                f"no scripted attempt {attempt_index} for task "
                f"{task.task_id!r}"
            )
        return candidate
