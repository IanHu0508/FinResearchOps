"""Thin command-line Adapter over the FinResearchOps Application Interface.

Every action is one call to `handle()` or `read_case()`.  The CLI carries no
financial, gating, review, or export rule of its own.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
from datetime import date
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Sequence

from finauditgate import FrozenDocumentPackage, RunRef
from finauditgate.core.operations import ANSWER_CONTRACT_VALUES
from finauditgate.adapters.ollama import OllamaModelAdapter
from finauditgate.application import (
    ApplicationError,
    ApplicationOutcome,
    CreateCase,
    ExportChangePacket,
    FinResearchOps,
    ReplayRun,
    ReviewAction,
    RunAnalysis,
    SubmitReview,
)
from finauditgate.core.profiles import PrivateDevValidationProfile
from finauditgate.private_storage import (
    PrivateStorageError,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finresearchops")
    parser.add_argument(
        "--artifact-root",
        required=True,
        type=Path,
        help="Private local root for Application and core artifacts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-case")
    create.add_argument("--question", required=True)
    create.add_argument("--cutoff", required=True, type=date.fromisoformat)
    create.add_argument("--document", required=True, type=Path)
    create.add_argument("--source-id", required=True)
    create.add_argument("--document-name")
    create.add_argument(
        "--published-at",
        required=True,
        type=date.fromisoformat,
    )
    create.add_argument(
        "--mode",
        choices=("SYNTHETIC_DEV", "PRIVATE_DEV", "POST_FREEZE_EVAL"),
        default="PRIVATE_DEV",
    )
    create.add_argument(
        "--answer-contract",
        choices=ANSWER_CONTRACT_VALUES,
        default="PERCENTAGE_CHANGE",
        help=(
            "what the question asks for. It must match the operation the "
            "validation profile allows, or the run is refused."
        ),
    )
    create.add_argument(
        "--risk-class",
        choices=("LOW", "MEDIUM", "MATERIAL"),
        default="LOW",
    )

    run = subparsers.add_parser("run-analysis")
    run.add_argument("--case-ref", required=True)
    run.add_argument(
        "--model-trace-root",
        required=True,
        type=Path,
        help="Private local root for content-addressed raw model traces.",
    )
    run.add_argument(
        "--validation-profile",
        type=Path,
        help=(
            "Canonical private-dev validation profile below the workspace "
            "private/ boundary (required to accept a PRIVATE_DEV case)."
        ),
    )

    inspect = subparsers.add_parser("inspect-case")
    inspect.add_argument("--case-ref", required=True)
    _add_optional_trace_root(inspect)

    review = subparsers.add_parser("review")
    review.add_argument("--case-ref", required=True)
    review.add_argument("--run-id", required=True)
    review.add_argument(
        "--action",
        required=True,
        choices=tuple(action.value for action in ReviewAction),
    )
    review.add_argument("--reason", required=True)
    _add_optional_trace_root(review)

    export = subparsers.add_parser("export")
    export.add_argument("--case-ref", required=True)
    export.add_argument("--run-id", required=True)
    _add_optional_trace_root(export)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--run-id", required=True)
    _add_optional_trace_root(replay)
    return parser


def _add_optional_trace_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-trace-root",
        type=Path,
        help="Required when inspecting or replaying a traced run.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse one CLI request and cross only the Application Interface."""

    arguments = _parser().parse_args(argv)
    try:
        arguments.private_workspace_anchor = resolve_private_workspace_anchor(
            arguments.artifact_root,
            purpose="artifact-root",
        )
        arguments.artifact_root = require_private_storage_root(
            arguments.artifact_root,
            purpose="artifact-root",
            anchor=arguments.private_workspace_anchor,
        )
        return _execute(arguments)
    except (ApplicationError, PrivateStorageError) as exc:
        error_code = (
            exc.code
            if isinstance(exc, ApplicationError)
            else "PRIVATE_STORAGE_REQUIRED"
        )
        print(
            json.dumps(
                {
                    "schema_version": "finresearchops.cli-error/v1",
                    "error": {
                        "family": "APPLICATION_ERROR",
                        "code": error_code,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


def _execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "create-case":
        application = FinResearchOps(
            artifact_root=arguments.artifact_root,
            private_workspace_anchor=arguments.private_workspace_anchor,
        )
        document_path = arguments.document
        if arguments.mode == "PRIVATE_DEV":
            document_path = require_private_storage_root(
                document_path,
                purpose="private-dev-document",
                anchor=arguments.private_workspace_anchor,
            )
            if not document_path.is_file():
                raise PrivateStorageError(
                    "PRIVATE_STORAGE_REQUIRED:private-dev-document:FILE_REQUIRED"
                )
        try:
            document_bytes = document_path.read_bytes()
        except OSError as exc:
            if arguments.mode == "PRIVATE_DEV":
                raise PrivateStorageError(
                    "PRIVATE_STORAGE_REQUIRED:private-dev-document:READ_FAILED"
                ) from exc
            raise ApplicationError("DOCUMENT_READ_FAILED") from exc
        outcome = application.handle(
            CreateCase(
                question=arguments.question,
                cutoff=arguments.cutoff,
                document=FrozenDocumentPackage(
                    source_id=arguments.source_id,
                    document_name=(
                        arguments.document_name or document_path.name
                    ),
                    document_bytes=document_bytes,
                    declared_published_at=arguments.published_at,
                ),
                risk_class=arguments.risk_class,
                mode=arguments.mode,
                answer_contract=arguments.answer_contract,
            )
        )
    elif arguments.command == "run-analysis":
        profile = None
        if arguments.validation_profile is not None:
            try:
                profile = PrivateDevValidationProfile.from_path(
                    arguments.validation_profile,
                    private_workspace_anchor=arguments.private_workspace_anchor,
                )
            except PrivateStorageError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise ApplicationError(
                    "PRIVATE_VALIDATION_PROFILE_INVALID"
                ) from exc
        model = OllamaModelAdapter(
            trace_root=arguments.model_trace_root,
            private_workspace_anchor=arguments.private_workspace_anchor,
        )
        application = FinResearchOps(
            artifact_root=arguments.artifact_root,
            model=model,
            model_trace_root=arguments.model_trace_root,
            private_dev_profile=profile,
            private_workspace_anchor=arguments.private_workspace_anchor,
        )
        outcome = application.handle(RunAnalysis(case_ref=arguments.case_ref))
    elif arguments.command == "inspect-case":
        application = _offline_application(arguments)
        _print_payload(_json_value(application.read_case(arguments.case_ref)))
        return 0
    elif arguments.command == "review":
        outcome = _offline_application(arguments).handle(
            SubmitReview(
                case_ref=arguments.case_ref,
                run_ref=RunRef(arguments.run_id),
                action=ReviewAction(arguments.action),
                reason=arguments.reason,
            )
        )
    elif arguments.command == "export":
        outcome = _offline_application(arguments).handle(
            ExportChangePacket(
                case_ref=arguments.case_ref,
                run_ref=RunRef(arguments.run_id),
            )
        )
    elif arguments.command == "replay":
        outcome = _offline_application(arguments).handle(
            ReplayRun(run_ref=RunRef(arguments.run_id))
        )
    else:
        raise AssertionError("argparse returned an unsupported command")
    _print_payload(_outcome_payload(outcome))
    return 0


def _offline_application(arguments: argparse.Namespace) -> FinResearchOps:
    """Reopen the Application without any model Adapter."""

    return FinResearchOps(
        artifact_root=arguments.artifact_root,
        model_trace_root=arguments.model_trace_root,
        private_workspace_anchor=arguments.private_workspace_anchor,
    )


def _outcome_payload(outcome: ApplicationOutcome) -> dict[str, object]:
    payload = {
        "operation": outcome.operation.value,
        "case_ref": outcome.case_ref,
        "status": outcome.status.value,
        "reason_codes": list(outcome.reason_codes),
    }
    optional = {
        "run_ref": (
            {"run_id": outcome.run_ref.run_id}
            if outcome.run_ref is not None
            else None
        ),
        "workpaper_ref": outcome.workpaper_ref,
        "review_ref": outcome.review_ref,
        "packet_ref": outcome.packet_ref,
        "replay_ref": outcome.replay_ref,
        "machine_decision": (
            outcome.machine_decision.value
            if outcome.machine_decision is not None
            else None
        ),
    }
    payload.update(
        {key: value for key, value in optional.items() if value is not None}
    )
    return payload


def _print_payload(payload: dict[str, object]) -> None:
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    raise SystemExit(main())
