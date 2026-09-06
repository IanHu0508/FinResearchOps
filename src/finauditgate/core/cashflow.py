"""Source-only financial checks and bounded investigation behind run/replay."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal, localcontext
import json
import re
from pathlib import Path

from finauditgate.cashflow import CashflowOutcome, CashflowTask
from finauditgate.contracts import Decision, FrozenDocumentPackage, ReplayReport, RunRef
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.ixbrl import FINANCIAL_CONTEXT, Filing
from finauditgate.core.operations import evaluate


SCHEMA = "finauditgate.cashflow-run/v3"
REPLAY_SCHEMA = "finauditgate.cashflow-replay/v1"
MAX_SEARCHES = 2
_CONTINUE_SEARCH = {'CANDIDATE_DISCLOSURES_FOUND', 'NO_MATCHING_DISCLOSURE', 'BACKGROUND_ONLY'}


def task_payload(task):
    payload = asdict(task)
    payload["document"].pop("document_bytes")
    payload["document"]["document_sha256"] = sha256_hex(task.document.document_bytes)
    payload["document"]["declared_published_at"] = task.document.declared_published_at.isoformat()
    for key in ("current_end", "comparison_end", "cutoff"):
        payload[key] = payload[key].isoformat()
    return payload


def _task_from_payload(payload, document):
    args = dict(payload)
    doc = dict(args.pop("document"))
    if sha256_hex(document) != doc.pop("document_sha256"):
        raise ValueError("SOURCE_HASH_MISMATCH")
    doc["document_bytes"] = document
    doc["declared_published_at"] = date.fromisoformat(doc["declared_published_at"])
    args["document"] = FrozenDocumentPackage(**doc)
    for key in ("current_end", "comparison_end", "cutoff"):
        args[key] = date.fromisoformat(args[key])
    return CashflowTask(**args)


def _pair(filing, row, task, concept=None, allow_partial=False):
    grouped = {task.current_end.isoformat(): [], task.comparison_end.isoformat(): []}
    for node, fact in filing.row_facts(row, task):
        if fact is not None and (concept is None or filing.is_concept(node, concept)):
            grouped[fact["period_end"]].append((node, fact))
    result = []
    for end in grouped:
        items = grouped[end]
        if not items:
            if not allow_partial:
                return None
            result.append(None)
            continue
        identities = {(x[1]["period_start"], x[1]["value"], x[1]["concept"]) for x in items}
        if len(identities) != 1:
            raise ValueError("AMBIGUOUS_ROW_FACTS")
        node, fact = items[0]
        fact = dict(fact)
        fact["displayed_cash_effect"] = filing.displayed_effect(row, node, fact)
        fact["row_label"] = filing.row_label(row)
        result.append(fact)
    return result if any(f is not None for f in result) else None


def _statement(filing, task):
    candidates = []
    for row in filing.rows:
        if not any(filing.is_concept(n, "NetCashProvidedByUsedInOperatingActivities") for n in row.descendants()):
            continue
        cfo = _pair(filing, row, task, "NetCashProvidedByUsedInOperatingActivities")
        if cfo is None:
            continue
        table = row.ancestor("table")
        preceding = [r for r in filing.rows if r.ancestor("table") is table and r.start < row.start]
        for start in reversed(preceding):
            if not any(filing.is_concept(n, "ProfitLoss") for n in start.descendants()):
                continue
            profit = _pair(filing, start, task, "ProfitLoss")
            if profit is None:
                continue
            rows = [r for r in preceding if r.start > start.start]
            candidates.append((profit, cfo, rows))
            break
    if not candidates:
        raise ValueError("CONSOLIDATED_CASHFLOW_STATEMENT_NOT_FOUND")
    signatures = {tuple((f["period_start"], f["period_end"], f["value"]) for f in p + c) for p, c, _ in candidates}
    if len(signatures) != 1:
        raise ValueError("CONFLICTING_CASHFLOW_STATEMENTS")
    return max(candidates, key=lambda c: len(c[2]))


def _difference(current, comparison):
    return format(evaluate("absolute_change", current=Decimal(current), comparison=Decimal(comparison),
                           quantize="0.01", precision=50, emin=-999, emax=999, capitals=1, clamp=0), "f")


def analyze(filing, task):
    result = {"schema_version": "finresearchops.cashflow-analysis/v3", "status": "PARTIAL",
              "currency": task.currency, "current_end": task.current_end.isoformat(),
              "comparison_end": task.comparison_end.isoformat(), "metrics": {},
              "drivers": [], "issues": [], "facts": [], "resolved_values": [],
              "operating_assets_liabilities": None, "issuer_overview": [], "management_check": None}
    if task.document.declared_published_at > task.cutoff:
        result["issues"].append("POST_CUTOFF_DOCUMENT")
        return result
    try:
        profit, cfo, rows = _statement(filing, task)
    except ValueError as exc:
        result["issues"].append(str(exc))
        return result
    if any(profit[i]["period_start"] != cfo[i]["period_start"] for i in (0, 1)):
        result["issues"].append("PROFIT_CASHFLOW_PERIOD_MISMATCH")
        return result
    if abs((task.current_end - task.comparison_end).days - 365) > 7:
        result["issues"].append("COMPARATIVE_YEAR_GAP_UNSUPPORTED")
        return result
    result["facts"] = profit + cfo
    for name, pair in (("profit", profit), ("operating_cashflow", cfo)):
        result["metrics"][name] = {"current": pair[0]["value"], "comparison": pair[1]["value"],
                                   "change": _difference(pair[0]["value"], pair[1]["value"]),
                                   "fact_ids": [f["fact_id"] for f in pair]}
    result["metrics"]["cash_minus_profit"] = {
        "current": _difference(cfo[0]["value"], profit[0]["value"]),
        "comparison": _difference(cfo[1]["value"], profit[1]["value"])}
    gap = result["metrics"]["cash_minus_profit"]
    gap["change"] = _difference(gap["current"], gap["comparison"])
    seen = set()
    working_capital_source = None
    working_capital_incomplete = False
    for row in rows:
        label = filing.row_label(row)
        if re.search(r'changes in operating assets and liabilities', label, re.I):
            working_capital_source = filing.reference(row)
            continue
        try:
            pair = _pair(filing, row, task, allow_partial=True)
        except ValueError as exc:
            result["issues"].append(str(exc))
            working_capital_incomplete |= working_capital_source is not None
            continue
        if pair is None:
            if working_capital_source is not None and re.search(r'[A-Za-z]', label) and re.search(r'[\d—–]', row.text()):
                working_capital_incomplete = True
            continue
        for index in (0, 1):
            if pair[index] is None and pair[1 - index] is not None:
                try:
                    pair[index] = filing.counterpart(pair[1 - index], profit[index], task, row)
                except ValueError as exc:
                    result['issues'].append(str(exc))
                if pair[index] is not None:
                    result['resolved_values'].append({'driver_id': f'driver-{row.start}',
                        'period_end': pair[index]['period_end'], 'fact_id': pair[index]['fact_id'],
                        'method': pair[index]['resolution']['method']})
        if any(pair[i] is not None and pair[i]["period_start"] != profit[i]["period_start"] for i in (0, 1)):
            result["issues"].append("ADJUSTMENT_PERIOD_MISMATCH")
            working_capital_incomplete |= working_capital_source is not None
            continue
        if any(f is None for f in pair):
            result["issues"].append("ADJUSTMENT_HAS_UNTAGGED_COMPARATIVE_VALUE")
        identity = tuple((f["concept"], f["period_end"], f["value"]) for f in pair if f is not None)
        if identity in seen:
            result["issues"].append("DUPLICATE_ADJUSTMENT_ROW")
            working_capital_incomplete |= working_capital_source is not None
            continue
        seen.add(identity)
        label = filing.row_label(row)
        driver = {"driver_id": f"driver-{row.start}", "label": label,
                  "current": None if pair[0] is None else pair[0]["displayed_cash_effect"],
                  "comparison": None if pair[1] is None else pair[1]["displayed_cash_effect"],
                  "change": None if any(f is None for f in pair) else _difference(pair[0]["displayed_cash_effect"], pair[1]["displayed_cash_effect"]),
                  "group": "OPERATING_ASSETS_LIABILITIES" if working_capital_source is not None else "OTHER_ADJUSTMENTS",
                  "fact_ids": [f["fact_id"] for f in pair if f is not None]}
        result["drivers"].append(driver)
        result["facts"].extend(f for f in pair if f is not None)
    with localcontext(FINANCIAL_CONTEXT):
        residuals, tolerances = {}, {}
        for i, period in enumerate(("current", "comparison")):
            amount = Decimal(cfo[i]["value"]) - Decimal(profit[i]["value"]) - sum((Decimal(d[period]) for d in result["drivers"] if d[period] is not None), Decimal(0))
            tolerance = sum((Decimal(f["uncertainty"]) for f in result["facts"] if f["period_end"] == (task.current_end if i == 0 else task.comparison_end).isoformat()), Decimal(0))
            residuals[period] = format(amount, "f")
            tolerances[period] = format(tolerance, "f")
            if abs(amount) > tolerance:
                result["issues"].append("CASHFLOW_BRIDGE_UNRECONCILED_" + period.upper())
        profit_change = Decimal(result["metrics"]["profit"]["change"])
        cash_change = Decimal(result["metrics"]["operating_cashflow"]["change"])
        result["direction"] = "DIVERGING" if profit_change * cash_change < 0 else "SAME_DIRECTION_OR_FLAT"
        result["signals"] = []
        if Decimal(profit[0]["value"]) > 0 and Decimal(cfo[0]["value"]) < 0:
            result["signals"].append("POSITIVE_PROFIT_NEGATIVE_OPERATING_CASH")
        if result["direction"] == "DIVERGING":
            result["signals"].append("PROFIT_AND_CASH_CHANGED_IN_OPPOSITE_DIRECTIONS")
        result["metrics"]["unexplained_residual"] = {**residuals, "change": _difference(residuals["current"], residuals["comparison"])}
        result["rounding_tolerance"] = tolerances
        if working_capital_source is not None:
            members = [d for d in result['drivers'] if d['group'] == 'OPERATING_ASSETS_LIABILITIES']
            complete = bool(members) and not working_capital_incomplete and all(d[p] is not None for d in members for p in ('current', 'comparison'))
            amounts = {p: format(sum((Decimal(d[p]) for d in members if d[p] is not None), Decimal(0)), 'f') for p in ('current', 'comparison')}
            result['operating_assets_liabilities'] = {**amounts,
                'change': _difference(amounts['current'], amounts['comparison']) if complete else None,
                'complete': complete, 'section_source': working_capital_source,
                'fact_ids': [fid for d in members for fid in d['fact_ids']]}
        result["drivers"].sort(key=lambda d: (-_priority(d), d["driver_id"]))
    result['issuer_overview'] = filing.search_notes('Operating cash flow', current_year=task.current_end.year, limit=2)
    result['management_check'] = _management_check(result, task.currency)
    # Ratios with small, zero, negative or sign-changing profit are deliberately
    # absent from this first task: the two values and monetary changes remain.
    result["issues"] = sorted(set(result["issues"]))
    result["status"] = "COMPLETED" if not result["issues"] else "PARTIAL"
    return result


def _management_check(analysis, currency):
    """Check a disclosed rounded working-capital movement against the CF section.

    This checks numerical compatibility only. It neither proves causation nor
    silently equates the issuer's definition with every possible NWC metric.
    """
    group = analysis['operating_assets_liabilities']
    if not group or not group['complete']:
        return None
    currency_pattern = {'CNY': r'(?:RMB|CNY)', 'USD': r'(?:USD|US\$)'}.get(currency)
    if currency_pattern is None:
        return None
    for hit in analysis['issuer_overview']:
        if hit['evidence_role'] != 'PERIOD_CHANGE_EXPLANATION':
            continue
        match = re.search(r'\b(net\s+)?(decrease|increase)\s+of\s+' + currency_pattern +
            r'\s*(\d+(?:\.\d+)?)\s+(billion|million|thousand)(?:\s*\([^)]{0,120}\))?\s+in\s+(?:changes in\s+)?working capital', hit['text'], re.I)
        if not match:
            continue
        with localcontext(FINANCIAL_CONTEXT):
            unit = {'billion': Decimal(10) ** 9, 'million': Decimal(10) ** 6, 'thousand': Decimal(10) ** 3}[match[4].lower()]
            reported = Decimal(match[3]) * unit * (-1 if match[2].lower() == 'decrease' else 1)
            places = len(match[3].partition('.')[2])
            tolerance = unit * Decimal('0.5') * Decimal(10) ** -places
            calculated = Decimal(group['change'])
            return {'note_id': hit['note_id'], 'quoted_amount_phrase': match[0],
                    'reported_change': format(reported, 'f'), 'calculated_change': group['change'],
                    'rounding_tolerance': format(tolerance, 'f'),
                    'within_rounding': abs(calculated - reported) <= tolerance,
                    'difference': format(calculated - reported, 'f')}
    return None


def _priority(driver):
    if driver["change"] is not None:
        return Decimal(driver["change"]).copy_abs()
    return max((Decimal(driver[p]).copy_abs() for p in ("current", "comparison") if driver[p] is not None), default=Decimal(0))


def observation(analysis, steps):
    return {"metrics": analysis["metrics"], "issues": analysis["issues"],
            "issuer_overview": [{"text": h["text"][:450], "evidence_role": h["evidence_role"]}
                                for h in analysis["issuer_overview"][:1]],
            "available_drivers": analysis["drivers"][:6],
            "previous_searches": [{"driver_id": s["action"].get("driver_id"),
                                   "hits": [{"note_id": h["note_id"], "text": h["text"][:220],
                                             "evidence_role": h['evidence_role']} for h in s.get("hits", [])],
                                   "feedback": s["feedback"]} for s in steps],
            "remaining_searches": MAX_SEARCHES - len(steps)}


def rule_action(obs):
    visited = {s["driver_id"] for s in obs["previous_searches"]}
    for driver in obs["available_drivers"]:
        if driver["driver_id"] not in visited and _priority(driver) != 0:
            return {"action": "SEARCH_NOTES", "driver_id": driver["driver_id"]}
    return {"action": "FINISH", "driver_id": "none"}


def choice_origin(strategy, steps):
    if strategy == 'rules':
        return 'RULES'
    if steps and steps[-1]['feedback'] in {'NO_MATCHING_DISCLOSURE', 'BACKGROUND_ONLY'}:
        return 'RULE_FALLBACK'
    return 'MODEL'


def perform(filing, analysis, steps, action):
    if type(action) is not dict or set(action) != {"action", "driver_id"}:
        return [], "INVALID_ACTION"
    if action["action"] == "FINISH" and action["driver_id"] == "none":
        return [], "FINISHED"
    drivers = {d["driver_id"]: d for d in analysis["drivers"][:6]}
    if action["action"] != "SEARCH_NOTES" or type(action["driver_id"]) is not str or action["driver_id"] not in drivers:
        return [], "ACTION_NOT_ALLOWED"
    if any(s["action"].get("driver_id") == action["driver_id"] for s in steps):
        return [], "REPEATED_SEARCH_STOPPED"
    hits = filing.search_notes(drivers[action["driver_id"]]["label"], current_year=analysis["current_end"][:4])
    if not hits:
        return [], 'NO_MATCHING_DISCLOSURE'
    useful = any(h['evidence_role'] in {'PERIOD_CHANGE_EXPLANATION', 'PERIOD_FACT', 'UNDATED_CHANGE_DESCRIPTION'} for h in hits)
    return hits, 'CANDIDATE_DISCLOSURES_FOUND' if useful else 'BACKGROUND_ONLY'


def run(task, root, investigator):
    filing = Filing(task.document.document_bytes)
    analysis = analyze(filing, task)
    steps = []
    if task.strategy == "adaptive" and investigator is None:
        raise ValueError("INVESTIGATOR_REQUIRED")
    if analysis["metrics"]:
        for _ in range(MAX_SEARCHES):
            obs = observation(analysis, steps)
            origin = choice_origin(task.strategy, steps)
            reply = ({"action": rule_action(obs), "trace": None} if origin != 'MODEL'
                     else investigator.choose(obs))
            action = reply["action"]
            hits, feedback = perform(filing, analysis, steps, action)
            steps.append({"action": action, "choice_origin": origin, "trace": reply["trace"], "hits": hits, "feedback": feedback})
            if feedback not in _CONTINUE_SEARCH:
                break
    record = {"schema_version": SCHEMA, "task": task_payload(task), "analysis": analysis,
              "steps": steps, "investigator": None if task.strategy == "rules" else investigator.identity,
              "review_status": "AWAITING_REVIEW", "decision": Decision.HUMAN_REVIEW.value}
    raw = canonical_json_bytes(record)
    ref = RunRef(sha256_hex(raw))
    directory = root / "runs" / ref.run_id
    write_once(root / "blobs" / "sha256" / filing.sha256, task.document.document_bytes)
    write_once(directory / "cashflow.json", raw)
    write_once(directory / "manifest.json", canonical_json_bytes({"schema_version": SCHEMA,
               "run_id": ref.run_id, "document_sha256": filing.sha256, "record_sha256": sha256_hex(raw)}))
    return CashflowOutcome(ref, Decision.HUMAN_REVIEW, filing.sha256, record)


def read_record(root, ref):
    raw = (root / "runs" / ref.run_id / "cashflow.json").read_bytes()
    if len(raw) > 4 * 1024 * 1024 or sha256_hex(raw) != ref.run_id:
        raise ValueError("CASHFLOW_RECORD_HASH_MISMATCH")
    record = json.loads(raw)
    if record["schema_version"] != SCHEMA or canonical_json_bytes(record) != raw:
        raise ValueError("CASHFLOW_RECORD_INVALID")
    return record


def replay(root, ref):
    try:
        record = read_record(root, ref)
        manifest = json.loads((root / "runs" / ref.run_id / "manifest.json").read_bytes())
        digest = record["task"]["document"]["document_sha256"]
        if manifest != {"schema_version": SCHEMA, "run_id": ref.run_id, "document_sha256": digest, "record_sha256": ref.run_id}:
            raise ValueError("CASHFLOW_MANIFEST_MISMATCH")
        data = (root / "blobs" / "sha256" / digest).read_bytes()
        task = _task_from_payload(record["task"], data)
        filing = Filing(data)
        analysis = analyze(filing, task)
        if analysis != record["analysis"] or len(record["steps"]) > MAX_SEARCHES:
            raise ValueError("CASHFLOW_RECALCULATION_MISMATCH")
        steps = []
        for step in record["steps"]:
            obs = observation(analysis, steps)
            origin = choice_origin(task.strategy, steps)
            if step.get('choice_origin') != origin:
                raise ValueError('CHOICE_ORIGIN_MISMATCH')
            if origin != 'MODEL':
                if step["trace"] is not None or step["action"] != rule_action(obs):
                    raise ValueError("RULE_ACTION_MISMATCH")
            else:
                from finauditgate.adapters.cashflow_ollama import verify_trace
                if not verify_trace(obs, step, record["investigator"]):
                    raise ValueError("INVESTIGATOR_TRACE_MISMATCH")
            hits, feedback = perform(filing, analysis, steps, step["action"])
            if hits != step["hits"] or feedback != step["feedback"]:
                raise ValueError("INVESTIGATION_REPLAY_MISMATCH")
            if steps and steps[-1]["feedback"] not in _CONTINUE_SEARCH:
                raise ValueError("ACTION_AFTER_STOP")
            steps.append(step)
        if not analysis["metrics"] and steps:
            raise ValueError("ACTION_WITHOUT_FINANCIAL_FACTS")
        if record["review_status"] != "AWAITING_REVIEW" or record["decision"] != Decision.HUMAN_REVIEW.value:
            raise ValueError("REVIEW_STATE_MISMATCH")
        return ReplayReport(REPLAY_SCHEMA, ref, True, Decision.HUMAN_REVIEW, None, None, 3)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return ReplayReport(REPLAY_SCHEMA, ref, False, None, None, None, 0,
                            str(exc) if isinstance(exc, ValueError) else "CASHFLOW_ARTIFACT_INVALID")
