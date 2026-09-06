"""Audited financial evidence behind the existing core run/replay Interface."""

import json
import re
from decimal import Decimal, localcontext

from finauditgate.cashflow import CashflowOutcome
from finauditgate.contracts import Decision, ReplayReport, RunRef
from finauditgate.core import cashflow
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.ixbrl import Filing, FINANCIAL_CONTEXT
from finauditgate.research import FundamentalEvidenceTask


SCHEMA = "finauditgate.research-evidence/v2"


def _disclosure_amounts(analysis, steps, currency):
    aliases = {"CNY": ("RMB", "CNY"), "USD": ("US$", "USD")}.get(currency, (currency,))
    prefix = "|".join(re.escape(x) for x in aliases)
    number = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    pattern = re.compile(rf"(?<![A-Za-z])(?:{prefix})\s*({number})\s*(billion|million|thousand)\b", re.I)
    notes = {hit["note_id"]: hit for hit in analysis["issuer_overview"]}
    notes.update({hit["note_id"]: hit for step in steps for hit in step["hits"]})
    result = []
    with localcontext(FINANCIAL_CONTEXT):
        for note_id, hit in notes.items():
            for match in pattern.finditer(hit["text"]):
                factor = {"billion": Decimal(10) ** 9, "million": Decimal(10) ** 6,
                          "thousand": Decimal(10) ** 3}[match[2].lower()]
                value = Decimal(match[1].replace(",", "")) * factor
                result.append({"note_id": note_id, "expression": match[0], "currency": currency,
                    "value": format(value, "f"), "note_text_start": match.start(), "note_text_end": match.end(),
                    "assurance": "EXPLICIT_AMOUNT_NORMALIZED_NOT_SEMANTIC_OR_FORECAST_APPROVAL"})
    return result


def _ratios(analysis):
    if not analysis["metrics"]:
        return None
    profit = analysis["metrics"]["profit"]
    cash = analysis["metrics"]["operating_cashflow"]
    with localcontext(FINANCIAL_CONTEXT):
        if any(Decimal(profit[period]) <= 0 for period in ("current", "comparison")):
            return None
        return {period: format((Decimal(cash[period]) / Decimal(profit[period])).quantize(Decimal("0.01")), "f")
                for period in ("current", "comparison")}


def _record(task):
    filing = Filing(task.filing.document.document_bytes)
    analysis = cashflow.analyze(filing, task.filing)
    steps = []
    for driver in task.driver_ids:
        if not analysis["metrics"]:
            raise ValueError("LOOKUP_WITHOUT_FINANCIAL_FACTS")
        action = {"action": "SEARCH_NOTES", "driver_id": driver}
        hits, feedback = cashflow.perform(filing, analysis, steps, action)
        if feedback not in cashflow._CONTINUE_SEARCH:
            raise ValueError("RESEARCH_DRIVER_NOT_ADMISSIBLE")
        steps.append({"action": action, "hits": hits, "feedback": feedback,
                      "choice_origin": "RESEARCH_REQUEST", "trace": None})
    return {"schema_version": SCHEMA, "task": cashflow.task_payload(task.filing),
            "driver_ids": list(task.driver_ids), "analysis": analysis, "steps": steps,
            "disclosure_amounts": _disclosure_amounts(analysis, steps, task.filing.currency),
            "cash_profit_ratios": _ratios(analysis),
            "review_status": "AWAITING_REVIEW", "decision": Decision.HUMAN_REVIEW.value}


def run(task, root):
    record = _record(task)
    raw = canonical_json_bytes(record)
    ref = RunRef(sha256_hex(raw))
    digest = record["task"]["document"]["document_sha256"]
    write_once(root / "blobs" / "sha256" / digest, task.filing.document.document_bytes)
    write_once(root / "runs" / ref.run_id / "research-evidence.json", raw)
    write_once(root / "runs" / ref.run_id / "manifest.json", canonical_json_bytes({
        "schema_version": SCHEMA, "run_id": ref.run_id, "document_sha256": digest}))
    return CashflowOutcome(ref, Decision.HUMAN_REVIEW, digest, record)


def read_record(root, ref):
    raw = (root / "runs" / ref.run_id / "research-evidence.json").read_bytes()
    if len(raw) > 4 * 1024 * 1024 or sha256_hex(raw) != ref.run_id:
        raise ValueError("RESEARCH_EVIDENCE_HASH_MISMATCH")
    record = json.loads(raw)
    if record.get("schema_version") != SCHEMA or canonical_json_bytes(record) != raw:
        raise ValueError("RESEARCH_EVIDENCE_SCHEMA_INVALID")
    return record


def replay(root, ref):
    try:
        record = read_record(root, ref)
        digest = record["task"]["document"]["document_sha256"]
        manifest = json.loads((root / "runs" / ref.run_id / "manifest.json").read_bytes())
        if manifest != {"schema_version": SCHEMA, "run_id": ref.run_id, "document_sha256": digest}:
            raise ValueError("RESEARCH_EVIDENCE_MANIFEST_MISMATCH")
        data = (root / "blobs" / "sha256" / digest).read_bytes()
        filing_task = cashflow._task_from_payload(record["task"], data)
        task = FundamentalEvidenceTask(filing_task, tuple(record["driver_ids"]))
        if _record(task) != record:
            raise ValueError("RESEARCH_EVIDENCE_RECALCULATION_MISMATCH")
        return ReplayReport("finauditgate.research-evidence-replay/v1", ref, True,
                            Decision.HUMAN_REVIEW, None, None, 3)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ReplayReport("finauditgate.research-evidence-replay/v1", ref, False,
                            None, None, None, 0,
                            str(exc) if isinstance(exc, ValueError) else "RESEARCH_EVIDENCE_INVALID")
