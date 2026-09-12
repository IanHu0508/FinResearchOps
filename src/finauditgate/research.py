"""Commands and read models for evidence-led investment research."""

from dataclasses import dataclass
from datetime import date
import re

from finauditgate.cashflow import CashflowTask, InterimCashflowTask
from finauditgate.contracts import RunRef


@dataclass(frozen=True, slots=True)
class FundamentalEvidenceTask:
    """The same source checks, with zero or up to two requested note lookups."""

    filing: CashflowTask | InterimCashflowTask
    driver_ids: tuple[str, ...] = ()
    include_owner_earnings: bool = False

    def __post_init__(self):
        if type(self.filing) not in (CashflowTask, InterimCashflowTask) or self.filing.strategy != "rules":
            raise ValueError("RULES_FILING_TASK_REQUIRED")
        if type(self.include_owner_earnings) is not bool or (self.include_owner_earnings and type(self.filing) is not InterimCashflowTask):
            raise ValueError("OWNER_EARNINGS_REQUIRES_INTERIM_FILING")
        if (type(self.driver_ids) is not tuple or len(self.driver_ids) > 2
                or any(type(x) is not str or not re.fullmatch(r"driver-\d+", x) for x in self.driver_ids)
                or len(set(self.driver_ids)) != len(self.driver_ids)):
            raise ValueError("AT_MOST_TWO_DISTINCT_DRIVERS")


@dataclass(frozen=True, slots=True)
class ResearchSecurity:
    filing: CashflowTask | InterimCashflowTask
    symbol: str
    question: str
    horizon_months: int = 12
    previous_case_ref: str | None = None

    def __post_init__(self):
        FundamentalEvidenceTask(self.filing)
        if type(self.symbol) is not str or not re.fullmatch(r"[A-Z0-9][A-Z0-9.^=-]{0,19}", self.symbol):
            raise ValueError("RESEARCH_SYMBOL_INVALID")
        if type(self.question) is not str or not 1 <= len(self.question.strip()) <= 1000:
            raise ValueError("RESEARCH_QUESTION_REQUIRED")
        if type(self.horizon_months) is not int or not 1 <= self.horizon_months <= 120:
            raise ValueError("RESEARCH_HORIZON_INVALID")
        if self.previous_case_ref is not None and (
                type(self.previous_case_ref) is not str
                or not re.fullmatch(r"case-[0-9a-f]{64}", self.previous_case_ref)):
            raise ValueError("PREVIOUS_RESEARCH_CASE_INVALID")


@dataclass(frozen=True, slots=True)
class ResearchCaseView:
    case_ref: str
    status: str
    run_refs: tuple[RunRef, ...]
    workpaper_paths: tuple[str, ...]
    latest_report: dict


@dataclass(frozen=True, slots=True)
class RunTradingBaseline:
    symbol: str
    as_of: date

    def __post_init__(self):
        if type(self.symbol) is not str or not re.fullmatch(r"[A-Z0-9][A-Z0-9.^=-]{0,19}", self.symbol):
            raise ValueError("RESEARCH_SYMBOL_INVALID")
        if type(self.as_of) is not date:
            raise ValueError("RESEARCH_DATE_REQUIRED")


@dataclass(frozen=True, slots=True)
class TradingBaselineView:
    case_ref: str
    status: str
    report_path: str
    latest_report: dict


@dataclass(frozen=True, slots=True)
class ResearchThesis:
    """Independent drafts and counterevidence updates, with optional aftercare."""

    symbol: str
    as_of: date
    question: str
    horizon_months: int = 12
    sources: dict | None = None
    review: bool = True
    hypotheses: tuple[str, ...] = ()
    research_constraints: tuple[str, ...] = ()
    user_view: str | None = None

    def __post_init__(self):
        RunTradingBaseline(self.symbol, self.as_of)
        if type(self.question) is not str or not 1 <= len(self.question.strip()) <= 1000:
            raise ValueError("RESEARCH_QUESTION_REQUIRED")
        if type(self.horizon_months) is not int or not 1 <= self.horizon_months <= 120:
            raise ValueError("RESEARCH_HORIZON_INVALID")
        if type(self.review) is not bool or (self.sources is not None and type(self.sources) is not dict):
            raise ValueError("THESIS_OPTIONS_INVALID")
        for values in (self.hypotheses, self.research_constraints):
            if (type(values) is not tuple or len(values) > 5
                    or any(type(v) is not str or not 1 <= len(v.strip()) <= 1000 for v in values)):
                raise ValueError("THESIS_RESEARCH_CONTEXT_INVALID")
        if self.user_view is not None and (type(self.user_view) is not str
                or not 1 <= len(self.user_view.strip()) <= 1000):
            raise ValueError("THESIS_USER_VIEW_INVALID")


def thesis_request(command):
    return {"symbol": command.symbol, "as_of": command.as_of.isoformat(),
            "question": command.question, "horizon_months": command.horizon_months,
            "data_mode": "FROZEN_SOURCES" if command.sources is not None else "LIVE_VENDOR",
            "hypotheses": list(command.hypotheses),
            "research_constraints": list(command.research_constraints), "user_view": command.user_view}


@dataclass(frozen=True, slots=True)
class ThesisCaseView:
    case_ref: str
    status: str
    report_path: str
    latest_report: dict
    review: dict


@dataclass(frozen=True, slots=True)
class RunAuditedNativeResearch:
    filing: CashflowTask | InterimCashflowTask
    symbol: str
    question: str
    horizon_months: int = 12
    market_task: "SecurityMarketTask | None" = None
    check_judgments: bool = False

    def __post_init__(self):
        ResearchSecurity(self.filing, self.symbol, self.question, self.horizon_months)
        if type(self.check_judgments) is not bool or (self.check_judgments and self.market_task is None):
            raise ValueError("JUDGMENT_MARKET_INPUT_REQUIRED")
        if self.market_task is not None and (type(self.market_task) is not SecurityMarketTask
                or self.market_task.symbol != self.symbol or self.market_task.identity_filing.cutoff != self.filing.cutoff
                or self.market_task.identity_filing.currency != self.filing.currency
                or int(self.market_task.identity_filing.entity_identifier) != int(self.filing.entity_identifier)):
            raise ValueError("NATIVE_SECURITY_TASK_MISMATCH")


@dataclass(frozen=True, slots=True)
class NativeResearchView:
    case_ref: str
    status: str
    run_ref: RunRef
    report_path: str
    latest_report: dict


@dataclass(frozen=True, slots=True)
class SecurityMarketTask:
    identity_filing: CashflowTask
    symbol: str
    quote_metadata: dict
    snapshot: str
    ohlcv_csv: bytes
    expected_session: date
    calendar_source: str

    def __post_init__(self):
        if type(self.identity_filing) is not CashflowTask:
            raise ValueError("ANNUAL_IDENTITY_FILING_REQUIRED")
        RunTradingBaseline(self.symbol, self.identity_filing.cutoff)
        if (type(self.quote_metadata) is not dict or type(self.snapshot) is not str or len(self.snapshot) > 100000
                or type(self.ohlcv_csv) is not bytes or not 0 < len(self.ohlcv_csv) <= 4*1024*1024
                or type(self.expected_session) is not date or self.expected_session > self.identity_filing.cutoff
                or type(self.calendar_source) is not str or not self.calendar_source.startswith("https://www.nasdaq.com/")):
            raise ValueError("SECURITY_MARKET_INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class ReviewNativeJudgment:
    financial_ref: RunRef
    market_ref: RunRef
    node: str
    proposal: dict
    prior_refs: tuple[RunRef, ...] = ()

    def __post_init__(self):
        if (type(self.financial_ref) is not RunRef or type(self.market_ref) is not RunRef
                or type(self.node) is not str or type(self.proposal) is not dict
                or type(self.prior_refs) is not tuple or len(self.prior_refs) > 10
                or any(type(r) is not RunRef for r in self.prior_refs)):
            raise ValueError("JUDGMENT_TASK_INVALID")
