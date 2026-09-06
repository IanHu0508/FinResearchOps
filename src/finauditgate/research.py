"""Commands and read models for evidence-led investment research."""

from dataclasses import dataclass
from datetime import date
import re

from finauditgate.cashflow import CashflowTask
from finauditgate.contracts import RunRef


@dataclass(frozen=True, slots=True)
class FundamentalEvidenceTask:
    """The same source checks, with zero or up to two requested note lookups."""

    filing: CashflowTask
    driver_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.filing) is not CashflowTask or self.filing.strategy != "rules":
            raise ValueError("RULES_FILING_TASK_REQUIRED")
        if (type(self.driver_ids) is not tuple or len(self.driver_ids) > 2
                or any(type(x) is not str or not re.fullmatch(r"driver-\d+", x) for x in self.driver_ids)
                or len(set(self.driver_ids)) != len(self.driver_ids)):
            raise ValueError("AT_MOST_TWO_DISTINCT_DRIVERS")


@dataclass(frozen=True, slots=True)
class ResearchSecurity:
    filing: CashflowTask
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
