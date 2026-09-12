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

    cashflow = subparsers.add_parser("investigate-cashflow", help="Investigate one acquired inline-XBRL filing and write a local draft.")
    cashflow.add_argument("--source-manifest", type=Path, help="Acquisition provenance JSON for an already acquired filing (not an answer profile).")
    cashflow.add_argument("--document", type=Path)
    cashflow.add_argument("--source-id")
    cashflow.add_argument("--source-url")
    cashflow.add_argument("--accession")
    cashflow.add_argument("--cik")
    cashflow.add_argument("--published-at", type=date.fromisoformat)
    cashflow.add_argument("--current-end", type=date.fromisoformat)
    cashflow.add_argument("--comparison-end", required=True, type=date.fromisoformat)
    cashflow.add_argument("--cutoff", required=True, type=date.fromisoformat)
    cashflow.add_argument("--currency", default="CNY")
    cashflow.add_argument("--strategy", choices=("rules", "adaptive"), default="rules")

    research = subparsers.add_parser("research-security", parents=[cashflow], add_help=False,
        help="Build an evidence-led operating thesis with the TradingAgents integration runtime.")
    research.add_argument("--symbol", required=True)
    research.add_argument("--question", required=True)
    research.add_argument("--horizon-months", type=int, default=12)
    research.add_argument("--previous-case-ref")
    research.add_argument("--source-format", choices=("annual-ixbrl", "interim-html"), default="annual-ixbrl",
                          help="Explicit January-June HTML 6-K reader; never an annual fallback.")
    _add_research_model_options(research)
    native = subparsers.add_parser("research-native", parents=[cashflow], add_help=False,
        help="Run the complete native chain with verified financial and market inputs.")
    native.add_argument("--symbol", required=True)
    native.add_argument("--question", required=True)
    native.add_argument("--horizon-months", type=int, default=12)
    native.add_argument("--source-format", choices=("annual-ixbrl","interim-html"), default="interim-html")
    native.add_argument("--identity-manifest", required=True, type=Path)
    native.add_argument("--market-inputs", required=True, type=Path)
    native.add_argument("--check-judgments",action="store_true",help="Require source-bound typed claims and manager adjudication at native node seams.")
    _add_research_model_options(native)
    native.set_defaults(max_spend_cny="unlimited",max_output_tokens=32768,reasoning_effort="high")
    baseline = subparsers.add_parser("tradingagents-baseline", help="Run the pinned native upstream graph on public vendor data.")
    baseline.add_argument("--symbol", required=True)
    baseline.add_argument("--as-of", required=True, type=date.fromisoformat)
    _add_research_model_options(baseline)
    thesis = subparsers.add_parser("research-thesis", help="Independent native drafts, counterevidence updates and optional nonblocking data review.")
    thesis.add_argument("--symbol", required=True)
    thesis.add_argument("--as-of", required=True, type=date.fromisoformat)
    thesis.add_argument("--question", required=True)
    thesis.add_argument("--hypothesis", action="append", default=[], help="A substantive hypothesis to test, not an established fact; repeat up to five times.")
    thesis.add_argument("--research-constraint", action="append", default=[], help="Explicit research horizon/risk or other mandate context; repeat up to five times.")
    thesis.add_argument("--user-view", help="Optional desired conclusion, recorded in the report but excluded from all main research requests. Keep factual reasons in --hypothesis or --sources.")
    thesis.add_argument("--horizon-months", type=int, default=12)
    thesis.add_argument("--sources", type=Path, help="Optional private frozen source bundle; without it native vendor tools are used.")
    thesis.add_argument("--no-review", action="store_true", help="Skip the post-report data review Agent; main research is unchanged.")
    thesis.add_argument("--resume-execution", type=Path, help="Reuse an interrupted frozen-source execution's exact completed model inputs/outputs.")
    thesis.add_argument("--reassess-final", action="store_true", help="With --resume-execution, preserve the exact completed prefix through forward assumptions, recalculate them, then obtain a new final assessment and optional review.")
    _add_research_model_options(thesis, synthesis=False)
    thesis.set_defaults(max_spend_cny="unlimited", max_output_tokens=65536, reasoning_effort="max")
    return parser


def _add_research_model_options(parser, *, synthesis=True):
    parser.add_argument("--model", choices=("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"), default="deepseek-flash")
    parser.add_argument("--env-file", type=Path, help="Optional private local configuration; never printed or recorded in model traces.")
    parser.add_argument("--max-spend-cny", default="50", help="Per-run conservative request reservation ceiling, or unlimited; not an account billing limit.")
    parser.add_argument("--max-output-tokens", type=int, default=8192, help="Maximum total generated tokens per request, up to 65536; enforced on DeepSeek's wire field.")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=None,
        help="Explicit DeepSeek thinking effort; omission preserves the provider default.")
    if synthesis:
        parser.add_argument("--synthesis-effort", choices=("low", "high", "max"), default=None,
            help="Optional effort override for the final evidence-only judgment.")


def _researcher(arguments):
    import os
    from uuid import uuid4
    from finauditgate.adapters.model_budget import ModelBudget
    from finauditgate.adapters.tradingagents_research import TradingAgentsResearcher
    _load_model_environment(arguments)
    flash = arguments.model in ("deepseek-flash", "deepseek-v4-flash")
    budget = ModelBudget(ceiling_cny=None if arguments.max_spend_cny == 'unlimited' else arguments.max_spend_cny,
        input_per_million="3" if flash else "9", output_per_million="9" if flash else "27",
        max_output_tokens=arguments.max_output_tokens,
        max_calls=24 if arguments.command == "tradingagents-baseline" else 4 if getattr(arguments, "previous_case_ref", None) else 3)
    return TradingAgentsResearcher(model=arguments.model, budget=budget, reasoning_effort=arguments.reasoning_effort,
        synthesis_effort=arguments.synthesis_effort,
        trace_root=arguments.artifact_root / "model-traces" / uuid4().hex)


def _load_model_environment(arguments):
    import os
    if arguments.env_file is not None:
        path = require_private_storage_root(arguments.env_file, purpose="model-configuration", anchor=arguments.private_workspace_anchor)
        if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise ApplicationError("MODEL_CONFIG_REQUIRES_PRIVATE_PERMISSIONS")
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if key.strip() != "DEEPSEEK_API_KEY" or not separator:
                raise ApplicationError("MODEL_CONFIG_FIELD_NOT_ALLOWED")
            value = value.strip().strip("\"'")
            os.environ["DEEPSEEK_API_KEY"] = value


def _native_signal(view):
    from finauditgate.application.native_case import JUDGMENT_SCHEMA
    if view.latest_report['schema_version']==JUDGMENT_SCHEMA:
        return {'investment_conclusion':'INSUFFICIENT_VALUATION_AND_PORTFOLIO_INPUT',
                'native_compatibility_signal':view.latest_report['result']['signal']}
    return {'signal':view.latest_report['result']['signal']}


def _native_market_task(arguments):
    from finauditgate.cashflow import CashflowTask
    from finauditgate.research import SecurityMarketTask
    from finauditgate.core.artifacts import sha256_hex
    path = require_private_storage_root(arguments.identity_manifest,purpose='identity-manifest',anchor=arguments.private_workspace_anchor)
    meta = json.loads(path.read_bytes())
    if meta.get('data_class') != 'PUBLIC_SOURCE_LOCAL' or meta.get('form') != '20-F' or Path(meta['stored_as']).name != meta['stored_as']:
        raise ValueError('IDENTITY_MANIFEST_INVALID')
    document_path = require_private_storage_root(path.parent/meta['stored_as'],purpose='identity-source',anchor=arguments.private_workspace_anchor)
    data = document_path.read_bytes()
    if sha256_hex(data) != meta['sha256']:
        raise ValueError('IDENTITY_SOURCE_HASH_MISMATCH')
    end=date.fromisoformat(meta['reporting_period_end'])
    filing=CashflowTask(meta['issuer'],FrozenDocumentPackage(meta['issuer'],document_path.name,data,date.fromisoformat(meta['publication_date_filed'])),
        meta['source_url'],meta['accession'],meta['cik'],end,end.replace(year=end.year-1),arguments.cutoff,arguments.currency)
    path=require_private_storage_root(arguments.market_inputs,purpose='market-inputs',anchor=arguments.private_workspace_anchor)
    market=json.loads(path.read_bytes())
    if market.get('schema_version')!='finresearchops.live-market-input/v1':
        raise ValueError('MARKET_INPUT_MANIFEST_INVALID')
    blobs={}
    for key in ('snapshot','ohlcv'):
        item=market[key]
        if Path(item['filename']).name!=item['filename']:
            raise ValueError('MARKET_INPUT_PATH_INVALID')
        p=require_private_storage_root(path.parent/item['filename'],purpose='market-source',anchor=arguments.private_workspace_anchor)
        blobs[key]=p.read_bytes()
        if sha256_hex(blobs[key])!=item['sha256']:
            raise ValueError('MARKET_INPUT_HASH_MISMATCH')
    return SecurityMarketTask(filing,arguments.symbol,market['quote_metadata'],blobs['snapshot'].decode(),blobs['ohlcv'],
                              date.fromisoformat(market['expected_session']),market['calendar_source'])


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
    if arguments.command == "research-thesis":
        from finauditgate.research import ResearchThesis
        from finauditgate.adapters.model_budget import ModelBudget
        from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
        try:
            _load_model_environment(arguments)
            bundle = None
            if arguments.sources is not None:
                source_path = require_private_storage_root(arguments.sources, purpose="thesis-sources", anchor=arguments.private_workspace_anchor)
                if source_path.stat().st_size > 256 * 1024:
                    raise ValueError("THESIS_SOURCE_BUNDLE_TOO_LARGE")
                bundle = json.loads(source_path.read_text())
            flash = arguments.model in ("deepseek-flash", "deepseek-v4-flash")
            resume = require_private_storage_root(arguments.resume_execution, purpose="thesis-resume", anchor=arguments.private_workspace_anchor) if arguments.resume_execution is not None else None
            budget = ModelBudget(ceiling_cny=None if arguments.max_spend_cny == "unlimited" else arguments.max_spend_cny,
                input_per_million="3" if flash else "9", output_per_million="9" if flash else "27",
                max_calls=24, max_input_bytes=524288, max_output_tokens=arguments.max_output_tokens)
            view = FinResearchOps(artifact_root=arguments.artifact_root,
                private_workspace_anchor=arguments.private_workspace_anchor,
                researcher=ThesisResearcher(model=arguments.model, live=True, budget=budget,
                                           reasoning_effort=arguments.reasoning_effort, resume_from=resume,
                                           reassess_final=arguments.reassess_final)).handle(
                    ResearchThesis(arguments.symbol, arguments.as_of, arguments.question,
                                   arguments.horizon_months, bundle, not arguments.no_review,
                                   tuple(arguments.hypothesis), tuple(arguments.research_constraint), arguments.user_view))
        except ImportError as exc:
            raise ApplicationError("TRADINGAGENTS_INTEGRATION_ENV_REQUIRED") from exc
        except ValueError as exc:
            raise ApplicationError(str(exc)) from exc
        _print_payload({"case_ref": view.case_ref, "status": view.status, "report": view.report_path,
                        "signal": view.latest_report["signal"], "data_review": view.review["status"],
                        "reused_model_calls": view.latest_report.get("reused_calls", {}).get("used_calls", 0),
                        "budget": view.review.get("budget_total", view.latest_report["budget"])})
        return 0
    if arguments.command == "tradingagents-baseline":
        from finauditgate.research import RunTradingBaseline
        try:
            view = FinResearchOps(artifact_root=arguments.artifact_root,
                private_workspace_anchor=arguments.private_workspace_anchor,
                researcher=_researcher(arguments)).handle(RunTradingBaseline(arguments.symbol, arguments.as_of))
        except ImportError as exc:
            raise ApplicationError("TRADINGAGENTS_INTEGRATION_ENV_REQUIRED") from exc
        except ValueError as exc:
            raise ApplicationError(str(exc)) from exc
        _print_payload({"case_ref": view.case_ref, "status": view.status, "report": view.report_path,
                        "signal": view.latest_report["signal"], "budget": view.latest_report["budget"]})
        return 0
    if arguments.command in ("investigate-cashflow", "research-security", "research-native"):
        from finauditgate.cashflow import CashflowTask, InterimCashflowTask, InvestigateCashflow
        from finauditgate.adapters.cashflow_ollama import OllamaCashflowPlanner
        try:
            if arguments.source_manifest is not None:
                manifest_path = require_private_storage_root(arguments.source_manifest, purpose="source-manifest", anchor=arguments.private_workspace_anchor)
                manifest = json.loads(manifest_path.read_bytes())
                interim = arguments.command in ("research-security","research-native") and arguments.source_format == "interim-html"
                if manifest.get("data_class") != "PUBLIC_SOURCE_LOCAL" or manifest.get("form") != ("6-K" if interim else "20-F"):
                    raise ValueError("ACQUIRED_20F_PROVENANCE_REQUIRED")
                stored_as = manifest["stored_as"]
                if Path(stored_as).name != stored_as:
                    raise ValueError("SOURCE_MANIFEST_FILENAME_INVALID")
                arguments.document = manifest_path.parent / stored_as
                arguments.source_id = manifest["issuer"]
                arguments.source_url = manifest["source_url"]
                arguments.accession = manifest["accession"]
                arguments.cik = manifest["cik"]
                arguments.published_at = date.fromisoformat(manifest["publication_date_filed"])
                arguments.current_end = date.fromisoformat(manifest["reporting_period_end"])
            elif any(getattr(arguments, key) is None for key in ("document", "source_id", "source_url", "accession", "cik", "published_at", "current_end")):
                raise ValueError("SOURCE_MANIFEST_OR_COMPLETE_METADATA_REQUIRED")
            document_path = require_private_storage_root(arguments.document, purpose="cashflow-document", anchor=arguments.private_workspace_anchor)
            data = document_path.read_bytes()
            if arguments.source_manifest is not None:
                from finauditgate.core.artifacts import sha256_hex
                if sha256_hex(data) != manifest["sha256"]:
                    raise ValueError("ACQUIRED_SOURCE_HASH_MISMATCH")
            task_type = InterimCashflowTask if arguments.command in ("research-security","research-native") and arguments.source_format == "interim-html" else CashflowTask
            task = task_type(arguments.source_id, FrozenDocumentPackage(arguments.source_id, document_path.name,
                data, arguments.published_at), arguments.source_url, arguments.accession,
                arguments.cik, arguments.current_end, arguments.comparison_end, arguments.cutoff,
                arguments.currency, arguments.strategy)
            if arguments.command == "research-native":
                from finauditgate.research import RunAuditedNativeResearch
                from finauditgate.adapters.tradingagents_native import NativeAuditAdapter
                from finauditgate.adapters.model_budget import ModelBudget
                market_task = _native_market_task(arguments)
                _load_model_environment(arguments)
                budget = ModelBudget(ceiling_cny=None if arguments.max_spend_cny == 'unlimited' else arguments.max_spend_cny,
                    input_per_million="3" if arguments.model in ("deepseek-flash", "deepseek-v4-flash") else "9",
                    output_per_million="9" if arguments.model in ("deepseek-flash", "deepseek-v4-flash") else "27",
                    max_calls=24,max_input_bytes=524288,max_output_tokens=arguments.max_output_tokens)
                adapter=NativeAuditAdapter({'deep_think_llm':arguments.model,'quick_think_llm':arguments.model,
                    'reasoning_effort':arguments.reasoning_effort},live=True,budget=budget)
                view=FinResearchOps(artifact_root=arguments.artifact_root,researcher=adapter,
                    private_workspace_anchor=arguments.private_workspace_anchor).handle(
                    RunAuditedNativeResearch(task,arguments.symbol,arguments.question,arguments.horizon_months,market_task,arguments.check_judgments))
                _print_payload({'case_ref':view.case_ref,'status':view.status,'report':view.report_path,
                    **_native_signal(view),'budget':view.latest_report['result']['budget']})
                return 0
            if arguments.command == "research-security":
                from finauditgate.research import ResearchSecurity
                view = FinResearchOps(artifact_root=arguments.artifact_root,
                    private_workspace_anchor=arguments.private_workspace_anchor,
                    researcher=_researcher(arguments)).handle(ResearchSecurity(task, arguments.symbol,
                        arguments.question, arguments.horizon_months, arguments.previous_case_ref))
                _print_payload({"case_ref": view.case_ref, "status": view.status,
                    "run_ids": [ref.run_id for ref in view.run_refs], "workpaper": view.workpaper_paths[0],
                    "outlook": view.latest_report["result"]["decision"]["outlook"],
                    "budget": (view.latest_report["update_explanation"]["budget"] if view.latest_report.get("update_explanation")
                               else view.latest_report["result"]["budget"])})
                return 0
            planner = OllamaCashflowPlanner() if arguments.strategy == "adaptive" else None
            view = FinResearchOps(artifact_root=arguments.artifact_root,
                private_workspace_anchor=arguments.private_workspace_anchor, investigator=planner).handle(InvestigateCashflow(task))
        except ImportError as exc:
            raise ApplicationError("TRADINGAGENTS_INTEGRATION_ENV_REQUIRED") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, PrivateStorageError):
                raise
            raise ApplicationError(str(exc) if isinstance(exc, ValueError) else "CASHFLOW_INPUT_OR_RUNTIME_UNAVAILABLE") from exc
        _print_payload({"case_ref": view.case_ref, "status": view.status,
                       "run_id": view.run_refs[0].run_id, "workpaper": view.workpaper_paths[0],
                       "analysis_status": view.latest_report["analysis"]["status"],
                       "issues": view.latest_report["analysis"]["issues"]})
        return 0
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
        from finauditgate.research import NativeResearchView, ThesisCaseView
        view = application.read_case(arguments.case_ref)
        if isinstance(view, ThesisCaseView):
            _print_payload({"case_ref": view.case_ref, "status": view.status, "report_path": view.report_path,
                            "signal": view.latest_report["signal"], "data_review": view.review["status"],
                            "reused_model_calls": view.latest_report.get("reused_calls", {}).get("used_calls", 0),
                            "budget": view.review.get("budget_total", view.latest_report["budget"])})
        elif isinstance(view, NativeResearchView):
            # Reopen verifies the complete trace; print a navigation summary
            # instead of dumping megabytes of every node's private messages.
            _print_payload({'case_ref':view.case_ref,'status':view.status,'report_path':view.report_path,
                'core_run_id':view.run_ref.run_id,'market_run_id':view.latest_report.get('market_run_id'),
                **_native_signal(view),'budget':view.latest_report['result'].get('budget')})
        else:
            _print_payload(_json_value(view))
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
        from finauditgate import ReplayReport
        if type(outcome) is ReplayReport:
            _print_payload(_json_value(outcome))
            return 0 if outcome.consistent else 2
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
