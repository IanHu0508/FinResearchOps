"""Task and result types for the filing-based cash-flow investigation.

There is deliberately no expected value, validation profile or answer key in
this Interface. The task supplies a filing and the periods to investigate.
"""

from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import urlsplit

from finauditgate.contracts import Decision, FrozenDocumentPackage, RunRef


@dataclass(frozen=True, slots=True)
class CashflowTask:
    task_id: str
    document: FrozenDocumentPackage
    source_url: str
    accession: str
    entity_identifier: str
    current_end: date
    comparison_end: date
    cutoff: date
    currency: str = "CNY"
    strategy: str = "rules"

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id.strip():
            raise ValueError("TASK_ID_REQUIRED")
        if type(self.document) is not FrozenDocumentPackage:
            raise TypeError("FROZEN_DOCUMENT_REQUIRED")
        for value in (self.current_end, self.comparison_end, self.cutoff):
            if type(value) is not date:
                raise TypeError("EXACT_DATE_REQUIRED")
        if not self.comparison_end < self.current_end <= self.cutoff:
            raise ValueError("PERIOD_ORDER_INVALID")
        if type(self.currency) is not str or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("ISO_CURRENCY_REQUIRED")
        if self.strategy not in ("rules", "adaptive"):
            raise ValueError("INVESTIGATION_STRATEGY_INVALID")
        if type(self.accession) is not str or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", self.accession):
            raise ValueError("ACCESSION_REQUIRED")
        if type(self.entity_identifier) is not str or not re.fullmatch(r"\d{1,10}", self.entity_identifier):
            raise ValueError("CIK_REQUIRED")
        url = urlsplit(self.source_url)
        expected = f"/Archives/edgar/data/{int(self.entity_identifier)}/{self.accession.replace('-', '')}/"
        if (url.scheme != "https" or url.netloc != "www.sec.gov"
                or not url.path.startswith(expected) or url.query or url.fragment):
            raise ValueError("SOURCE_URL_ACCESSION_MISMATCH")
        if not 0 < len(self.document.document_bytes) <= 32 * 1024 * 1024:
            raise ValueError("FILING_SIZE_UNSUPPORTED")


@dataclass(frozen=True, slots=True)
class CashflowOutcome:
    run_ref: RunRef
    decision: Decision
    document_sha256: str
    report: dict


@dataclass(frozen=True, slots=True)
class InvestigateCashflow:
    """One Application command to create a Case and its draft workpaper."""

    task: CashflowTask

    def __post_init__(self) -> None:
        if type(self.task) is not CashflowTask:
            raise TypeError("CASHFLOW_TASK_REQUIRED")


@dataclass(frozen=True, slots=True)
class CashflowCaseView:
    case_ref: str
    status: str
    run_refs: tuple[RunRef, ...]
    workpaper_paths: tuple[str, ...]
    latest_report: dict
