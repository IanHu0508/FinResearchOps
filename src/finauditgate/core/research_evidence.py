"""Audited financial evidence behind the existing core run/replay Interface."""

import json
import re
from decimal import Decimal, localcontext

from finauditgate.cashflow import CashflowOutcome, InterimCashflowTask
from finauditgate.contracts import Decision, ReplayReport, RunRef
from finauditgate.core import cashflow
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.ixbrl import Filing, FINANCIAL_CONTEXT
from finauditgate.research import FundamentalEvidenceTask


SCHEMA = "finauditgate.research-evidence/v3"
RELEASED_SCHEMA = "finauditgate.research-evidence/v2"
INTERIM_SCHEMA = "finauditgate.research-evidence/v5"
PREVIOUS_INTERIM_SCHEMA = "finauditgate.research-evidence/v4"
OWNER_EARNINGS_SCHEMA = "finauditgate.research-evidence/v6"


def _reference_meanings(analysis):
    """Bind statement role and duration to each reference, without judging prose."""
    totals = {}
    for key, kind, label in (("profit", "ANNUAL_NET_INCOME", "年度合并净利润"),
                             ("operating_cashflow", "ANNUAL_OPERATING_CASH_FLOW", "年度经营现金流净额")):
        for ref in analysis["metrics"].get(key, {}).get("fact_ids", []):
            totals[ref] = (kind, label)
    meanings = {}
    for fact in analysis["facts"]:
        kind, label = totals.get(fact["fact_id"], ("ANNUAL_RECONCILIATION_COMPONENT", "年度利润至经营现金流调节额"))
        limit = ("这是期间数，不代表期末余额。" if fact["fact_id"] in totals else
                 "年度调节额不代表期末余额、客户毛收款或独立现金收付；正向加回不证明正收益。原始科目符号与报表调节方向须分别理解，不由符号推断现金分红、经常性或未来现金结果。")
        meanings[fact["fact_id"]] = {"measure_kind": kind, "label": label,
            "periods": [{"start": fact["period_start"], "end": fact["period_end"]}], "use_limit": limit}
    for driver in analysis["drivers"]:
        periods = [period for ref in driver["fact_ids"] for period in meanings[ref]["periods"]]
        meanings[driver["driver_id"]] = {"measure_kind": "ANNUAL_RECONCILIATION_COMPARISON",
            "label": "两期年度调节额及其同比差额", "periods": periods,
            "use_limit": "同比差额比较的是年度调节额，不代表余额增速或客户毛收款增速；加回增加不证明投资收益增加。"}
    return meanings


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


def _record(task, schema=None):
    interim = type(task.filing) is InterimCashflowTask
    schema = schema or (OWNER_EARNINGS_SCHEMA if task.include_owner_earnings else INTERIM_SCHEMA if interim else SCHEMA)
    if interim:
        from finauditgate.core.interim import InterimFiling, analyze
        if task.filing.document.declared_published_at > task.filing.cutoff:
            raise ValueError("POST_CUTOFF_DOCUMENT")
        filing = InterimFiling(task.filing.document.document_bytes, task.filing,
            include_profit=schema in (INTERIM_SCHEMA, OWNER_EARNINGS_SCHEMA),
            include_owner_earnings=schema == OWNER_EARNINGS_SCHEMA)
        analysis = analyze(filing, task.filing)
    else:
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
    record = {"schema_version": schema, "task": cashflow.task_payload(task.filing),
            "driver_ids": list(task.driver_ids), "analysis": analysis, "steps": steps,
            "disclosure_amounts": _disclosure_amounts(analysis, steps, task.filing.currency),
            "cash_profit_ratios": _ratios(analysis),
            "review_status": "AWAITING_REVIEW", "decision": Decision.HUMAN_REVIEW.value}
    if schema in (SCHEMA, INTERIM_SCHEMA, PREVIOUS_INTERIM_SCHEMA, OWNER_EARNINGS_SCHEMA):
        record["reference_meanings"] = _reference_meanings(analysis)
    if interim:
        record["task"]["source_format"] = "INTERIM_HTML_JANUARY_JUNE"
        for fact in analysis["facts"]:
            kind = fact["measurement_kind"]
            old = record["reference_meanings"][fact["fact_id"]]
            old.update(measure_kind=kind, label={
                "HALF_YEAR_NET_INCOME": "半年合并净利润", "HALF_YEAR_OPERATING_CASH_FLOW": "半年经营现金流净额",
                "HALF_YEAR_CASH_TAX_PAYMENT": "半年实付所得税净额", "HALF_YEAR_TAX_EXPENSE": "半年所得税费用",
                "INSTANT_LIABILITY_BALANCE": "期末合同负债余额",
                "HALF_YEAR_PARENT_NET_INCOME": "半年归属于母公司股东净利润",
                "HALF_YEAR_SIGNED_ATTRIBUTION_ADJUSTMENT": "半年合并至归母净利润的报表带符号调整",
                "HALF_YEAR_INCOME_STATEMENT_VALUE": "半年利润表项目（按报表符号）"}.get(kind, "半年利润至经营现金流调节额"))
            old["use_limit"] = ("这是指定日期余额，不等于现金流量表期间调节或客户毛收款。" if kind == "INSTANT_LIABILITY_BALANCE"
                else "实付税款与同期费用的比较不是完整税务勾稽，不能仅由递延税符号推断现金税负。" if "TAX" in kind
                else "利润表项目用于利润变动核算，不与现金流调节项混为同一贡献；税费负数不表示同期现金支付。" if kind == "HALF_YEAR_INCOME_STATEMENT_VALUE"
                else "归母净利润与合并净利润不同，归属调整不是现金收付；未提供每股、ADS、汇率或全年换算。" if kind in ("HALF_YEAR_PARENT_NET_INCOME", "HALF_YEAR_SIGNED_ATTRIBUTION_ADJUSTMENT")
                else old["use_limit"].replace("年度", "半年"))
        for driver in analysis["drivers"]:
            meaning = record["reference_meanings"][driver["driver_id"]]
            meaning["measure_kind"] = "HALF_YEAR_RECONCILIATION_COMPARISON"
            meaning["label"] = meaning["label"].replace("年度", "半年")
            meaning["use_limit"] = meaning["use_limit"].replace("年度", "半年")
    return record


def run(task, root):
    record = _record(task)
    raw = canonical_json_bytes(record)
    ref = RunRef(sha256_hex(raw))
    digest = record["task"]["document"]["document_sha256"]
    write_once(root / "blobs" / "sha256" / digest, task.filing.document.document_bytes)
    write_once(root / "runs" / ref.run_id / "research-evidence.json", raw)
    write_once(root / "runs" / ref.run_id / "manifest.json", canonical_json_bytes({
        "schema_version": record["schema_version"], "run_id": ref.run_id, "document_sha256": digest}))
    return CashflowOutcome(ref, Decision.HUMAN_REVIEW, digest, record)


def read_record(root, ref):
    raw = (root / "runs" / ref.run_id / "research-evidence.json").read_bytes()
    if len(raw) > 4 * 1024 * 1024 or sha256_hex(raw) != ref.run_id:
        raise ValueError("RESEARCH_EVIDENCE_HASH_MISMATCH")
    record = json.loads(raw)
    if record.get("schema_version") not in (SCHEMA, RELEASED_SCHEMA, INTERIM_SCHEMA, PREVIOUS_INTERIM_SCHEMA, OWNER_EARNINGS_SCHEMA) or canonical_json_bytes(record) != raw:
        raise ValueError("RESEARCH_EVIDENCE_SCHEMA_INVALID")
    return record


def replay(root, ref):
    try:
        record = read_record(root, ref)
        digest = record["task"]["document"]["document_sha256"]
        manifest = json.loads((root / "runs" / ref.run_id / "manifest.json").read_bytes())
        if manifest != {"schema_version": record["schema_version"], "run_id": ref.run_id, "document_sha256": digest}:
            raise ValueError("RESEARCH_EVIDENCE_MANIFEST_MISMATCH")
        data = (root / "blobs" / "sha256" / digest).read_bytes()
        payload = dict(record["task"])
        if record["schema_version"] in (INTERIM_SCHEMA, PREVIOUS_INTERIM_SCHEMA, OWNER_EARNINGS_SCHEMA):
            if payload.pop("source_format") != "INTERIM_HTML_JANUARY_JUNE":
                raise ValueError("INTERIM_SOURCE_FORMAT_INVALID")
            from dataclasses import fields
            base = cashflow._task_from_payload(payload, data)
            filing_task = InterimCashflowTask(**{f.name: getattr(base, f.name) for f in fields(base)})
        else:
            filing_task = cashflow._task_from_payload(payload, data)
        task = FundamentalEvidenceTask(filing_task, tuple(record["driver_ids"]), record["schema_version"] == OWNER_EARNINGS_SCHEMA)
        if _record(task, record["schema_version"]) != record:
            raise ValueError("RESEARCH_EVIDENCE_RECALCULATION_MISMATCH")
        return ReplayReport("finauditgate.research-evidence-replay/v1", ref, True,
                            Decision.HUMAN_REVIEW, None, None, 3)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ReplayReport("finauditgate.research-evidence-replay/v1", ref, False,
                            None, None, None, 0,
                            str(exc) if isinstance(exc, ValueError) else "RESEARCH_EVIDENCE_INVALID")
