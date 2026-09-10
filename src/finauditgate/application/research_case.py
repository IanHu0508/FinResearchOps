"""Investment research Cases: core-owned facts, bounded drafts, old thesis last."""

import json
import re
from decimal import Decimal, localcontext
from html import escape
from uuid import uuid4

from finauditgate.adapters.research_contract import clone, model_evidence_view, required_review_references, selected_drivers, validate_proposal, update_payload, validate_update
from finauditgate.application.contracts import ApplicationError
from finauditgate.application.cashflow_report import _amount, _page, render as render_financial
from finauditgate.contracts import RunRef
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.research_evidence import read_record
from finauditgate.core.ixbrl import FINANCIAL_CONTEXT
from finauditgate.research import FundamentalEvidenceTask, ResearchCaseView
from finauditgate.cashflow import InterimCashflowTask


SCHEMA = "finresearchops.investment-research-case/v5"
INTERIM_SCHEMA = "finresearchops.investment-research-case/v6"
EXPLAINED_SCHEMA = "finresearchops.investment-research-case/v7"


def _validate_update_receipt(receipt, payload):
    if set(receipt) != {"proposal", "call", "budget"}:
        raise ValueError("RESEARCH_UPDATE_SHAPE_INVALID")
    validate_update(receipt["proposal"], payload)
    if (receipt["call"] != {"stage": "update", "request": payload, "proposal": receipt["proposal"]}
            or receipt["budget"].get("calls") != 4):
        raise ValueError("RESEARCH_UPDATE_CONTEXT_CHANGED")


def request_for(command):
    filing = command.filing
    return {"symbol": command.symbol, "entity_identifier": str(int(filing.entity_identifier)),
            "question": command.question, "horizon_months": command.horizon_months,
            "cutoff": filing.cutoff.isoformat(), "source_id": filing.document.source_id,
            "scope": ("INTERIM_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION" if type(filing) is InterimCashflowTask
                      else "ANNUAL_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION")}


def _validate_result(result, request, records):
    if set(result) != {"analysis", "challenge", "decision", "calls", "budget", "upstream_commit", "runtime_kind"}:
        raise ValueError("RESEARCH_RESULT_SHAPE_INVALID")
    initial, final = records[0], records[-1]
    task = initial["task"]
    scope = ("INTERIM_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION" if task.get("source_format")
             else "ANNUAL_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION")
    if request["scope"] != scope:
        raise ValueError("RESEARCH_SCOPE_SOURCE_MISMATCH")
    if (any(record["task"] != task for record in records)
            or request["entity_identifier"] != str(int(task["entity_identifier"]))
            or request["source_id"] != task["document"]["source_id"]
            or request["cutoff"] != task["cutoff"]):
        raise ValueError("RESEARCH_SOURCE_TASK_MISMATCH")
    if type(result["budget"].get("calls")) is not int or result["budget"]["calls"] != 3:
        raise ValueError("RESEARCH_CALL_RECEIPT_INVALID")
    if (result["upstream_commit"] != "2448d0a12576f9b2ddcd5980a0630833423d1e1b"
            or result["runtime_kind"] not in ("SCRIPTED_INTEGRATION", "TRADINGAGENTS_MODEL_CLIENT")):
        raise ValueError("RESEARCH_RUNTIME_IDENTITY_INVALID")
    validate_proposal(result["analysis"], initial)
    validate_proposal(result["challenge"], initial)
    validate_proposal(result["decision"], final, final=True, challenge=result["challenge"])
    drivers = selected_drivers(result["analysis"], result["challenge"], initial)
    if (len(records) != (2 if drivers else 1)
            or final["driver_ids"] != list(drivers) or initial["driver_ids"]):
        raise ValueError("RESEARCH_LOOKUP_HISTORY_INVALID")
    expected = []
    for stage in ("analysis", "challenge", "synthesis"):
        payload = {"request": request, "evidence": model_evidence_view(initial if stage != "synthesis" else final)}
        if stage == "synthesis":
            payload["required_evidence_ids"] = required_review_references(result["challenge"], final)
        expected.append({"stage": stage, "request": payload,
                         "proposal": result["decision"] if stage == "synthesis" else result[stage]})
    if result["calls"] != expected:
        raise ValueError("RESEARCH_CONTEXT_OR_STAGE_CHANGED")


def _source_view(evidence):
    a, t = evidence["analysis"], evidence["task"]
    contract = next((d for d in a["drivers"] if d["label"].lower() == "contract liabilities"), None)
    return {"document_sha256": t["document"]["document_sha256"],
            "published_at": t["document"]["declared_published_at"], "current_end": t["current_end"],
            "comparison_end": t["comparison_end"], "currency": a["currency"],
            "period_basis": "半年同比" if t.get("source_format") else "全年同比",
            "profit_change": a["metrics"]["profit"]["change"],
            "cashflow_change": a["metrics"]["operating_cashflow"]["change"],
            "contract_adjustment_current": None if contract is None else contract["current"]}


def _comparison(request, new_decision, previous, evidence=None, previous_evidence=None):
    if previous is None:
        result = {"previous_case_ref": None, "kind": "INITIAL_RESEARCH", "previous": None,
                "current": new_decision, "note": "首次建立论点。"}
        if evidence is not None:
            result["source_update"] = {"kind": "INITIAL_SOURCE", "previous": None, "current": _source_view(evidence),
                                       "availability_relation": "FIRST_RESEARCH"}
        return result
    old_request = previous.latest_report["request"]
    same = ("symbol", "entity_identifier", "question", "horizon_months")
    allowed_scopes = {"ANNUAL_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION", "INTERIM_EARNINGS_CASHFLOW_THESIS_NOT_COMPLETE_VALUATION"}
    if (evidence is None and request["scope"] != old_request["scope"]) or not {request["scope"], old_request["scope"]} <= allowed_scopes:
        raise ValueError("PREVIOUS_RESEARCH_TASK_MISMATCH")
    if any(request[k] != old_request[k] for k in same) or old_request["cutoff"] > request["cutoff"]:
        raise ValueError("PREVIOUS_RESEARCH_TASK_MISMATCH")
    old = previous.latest_report["result"]["decision"]
    result = {"previous_case_ref": previous.case_ref,
            "kind": "OUTLOOK_CHANGED" if old["outlook"] != new_decision["outlook"] else "OUTLOOK_RETAINED",
            "previous": old, "current": new_decision,
            "note": "先形成本次判断，再逐字段展示旧论点。文字差异不自动等于金融假设已发生变化。"}
    if evidence is not None:
        before, after = _source_view(previous_evidence), _source_view(evidence)
        result["source_update"] = {"kind": "NEW_SOURCE" if before["document_sha256"] != after["document_sha256"] else "SAME_SOURCE_RERUN",
                                   "previous": before, "current": after,
                                   "availability_relation": "ALREADY_PUBLIC_AT_PREVIOUS_CUTOFF" if after["published_at"] <= old_request["cutoff"] else "PUBLISHED_AFTER_PREVIOUS_CUTOFF"}
    return result


def run(application, command):
    if application._researcher is None:
        raise ApplicationError("RESEARCH_MODEL_REQUIRED")
    application._require_private_artifact_root()
    request = request_for(command)
    refs, records = [], []
    execution = application._application_root / "research-executions" / uuid4().hex
    if command.previous_case_ref and not (
            application._application_root / "research-cases" / command.previous_case_ref / "case.json").is_file():
        raise ApplicationError("PREVIOUS_RESEARCH_CASE_NOT_FOUND")

    def audited(drivers):
        if len(records) >= 2 or (records and not drivers):
            raise ValueError("RESEARCH_LOOKUP_BUDGET_EXHAUSTED")
        outcome = application._gate.run(FundamentalEvidenceTask(command.filing, drivers))
        refs.append(outcome.run_ref)
        records.append(clone(outcome.report))
        return clone(outcome.report)

    try:
        initial = audited(())
        if not initial["analysis"]["metrics"]:
            raise ValueError("RESEARCH_CORE_INPUT_UNAVAILABLE")
        result = clone(application._researcher.run(clone(request), initial, audited))
        _validate_result(result, request, records)
        # This write happens before retrieving any previous research content.
        write_once(execution / "new-decision.json", canonical_json_bytes({
            "schema_version": "finresearchops.new-research-decision/v1",
            "request": request, "decision": result["decision"]}))
        previous = application.read_case(command.previous_case_ref) if command.previous_case_ref else None
        if previous is not None and type(previous) is not ResearchCaseView:
            raise ValueError("PREVIOUS_RESEARCH_CASE_REQUIRED")
        interim = type(command.filing) is InterimCashflowTask
        previous_evidence = read_record(application._core_root, previous.run_refs[-1]) if interim and previous else None
        comparison = _comparison(request, result["decision"], previous,
                                 records[-1] if interim else None, previous_evidence)
        explained = records[-1]["schema_version"] == "finauditgate.research-evidence/v5"
        update = None
        if explained and previous:
            payload = update_payload(request, previous.latest_report, result["decision"], records[-1], _source_view(previous_evidence))
            saved = canonical_json_bytes(result["decision"])
            update = clone(application._researcher.explain_update(clone(payload)))
            _validate_update_receipt(update, payload)
            if canonical_json_bytes(result["decision"]) != saved:
                raise ValueError("RESEARCH_UPDATE_CHANGED_NEW_DECISION")
        record = {"schema_version": EXPLAINED_SCHEMA if explained else INTERIM_SCHEMA if interim else SCHEMA, "request": request, "result": result,
                  "run_ids": [ref.run_id for ref in refs], "comparison": comparison,
                  "review_status": "AWAITING_REVIEW"}
        if explained:
            record["update_explanation"] = update
        raw = canonical_json_bytes(record)
        case_ref = "case-" + sha256_hex(raw)
        directory = application._application_root / "research-cases" / case_ref
        write_once(directory / "case.json", raw)
        for name, value in render(record, records[-1]).items():
            write_once(directory / name, value)
        return application.read_case(case_ref)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "RESEARCH_INPUT_OR_MODEL_FAILED"
        write_once(execution / "failure.json", canonical_json_bytes({
            "schema_version": "finresearchops.research-failure/v1", "request": request,
            "core_run_ids": [ref.run_id for ref in refs], "error": code}))
        raise ApplicationError(code) from exc


def load(application, case_ref):
    directory = application._application_root / "research-cases" / case_ref
    try:
        raw = (directory / "case.json").read_bytes()
        if len(raw) > 4 * 1024 * 1024 or "case-" + sha256_hex(raw) != case_ref:
            raise ValueError("RESEARCH_CASE_HASH_MISMATCH")
        record = json.loads(raw)
        if (record.get("schema_version") not in (SCHEMA, INTERIM_SCHEMA, EXPLAINED_SCHEMA) or canonical_json_bytes(record) != raw
                or record.get("review_status") != "AWAITING_REVIEW"
                or not 1 <= len(record["run_ids"]) <= 2):
            raise ValueError("RESEARCH_CASE_SCHEMA_INVALID")
        refs, records = [], []
        for run_id in record["run_ids"]:
            ref = RunRef(run_id)
            if not application._offline_gate.replay(ref).consistent:
                raise ValueError("RESEARCH_CORE_REPLAY_FAILED")
            refs.append(ref)
            records.append(read_record(application._core_root, ref))
        _validate_result(record["result"], record["request"], records)
        previous_ref = record["comparison"]["previous_case_ref"]
        previous = application.read_case(previous_ref) if previous_ref else None
        interim = record["schema_version"] in (INTERIM_SCHEMA, EXPLAINED_SCHEMA)
        if interim != (records[-1]["task"].get("source_format") == "INTERIM_HTML_JANUARY_JUNE"):
            raise ValueError("RESEARCH_CASE_EVIDENCE_FORMAT_MISMATCH")
        previous_evidence = read_record(application._core_root, previous.run_refs[-1]) if interim and previous else None
        if record["comparison"] != _comparison(record["request"], record["result"]["decision"], previous,
                                               records[-1] if interim else None, previous_evidence):
            raise ValueError("RESEARCH_PRIOR_COMPARISON_CHANGED")
        if record["schema_version"] == EXPLAINED_SCHEMA:
            if records[-1]["schema_version"] != "finauditgate.research-evidence/v5":
                raise ValueError("RESEARCH_EXPLANATION_EVIDENCE_VERSION")
            if previous:
                payload = update_payload(record["request"], previous.latest_report, record["result"]["decision"], records[-1], _source_view(previous_evidence))
                _validate_update_receipt(record["update_explanation"], payload)
            elif record["update_explanation"] is not None:
                raise ValueError("RESEARCH_UPDATE_WITHOUT_PRIOR_CASE")
        for name, expected in render(record, records[-1]).items():
            if (directory / name).read_bytes() != expected:
                raise ValueError("RESEARCH_REPORT_CHANGED")
        return ResearchCaseView(case_ref, "AWAITING_REVIEW", tuple(refs),
                                (str(directory / "workpaper.html"),), record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ApplicationError("RESEARCH_CASE_INTEGRITY_FAILED") from exc


def _refs(ids):
    return " ".join(f"<a href='evidence.html#{escape(ref, quote=True)}'>[{i + 1}]</a>" for i, ref in enumerate(ids))


def _claims(claims):
    return "".join("<article><p><strong>" + escape(c["statement"]) + "</strong> " + _refs(c["evidence_ids"])
        + "</p><p>假设：" + escape(c["assumption"]) + "</p><p>重新判断的条件："
        + escape(c["revisit_if"]) + "</p></article>" for c in claims)


def _money(value, currency):
    if value is None:
        return "未提取"
    with localcontext(FINANCIAL_CONTEXT):
        divisor = Decimal(100000000) if currency == "CNY" else Decimal(1000000)
        return format(Decimal(value) / divisor, ",.2f")


def _meaning(evidence, reference):
    meaning = evidence.get("reference_meanings", {}).get(reference)
    if meaning is None:
        return ""
    periods = "；".join((p["start"] + " 至 " if p["start"] else "") + p["end"] for p in meaning["periods"])
    return ("<small>核验口径：" + escape(meaning["label"]) + "。期间：" + escape(periods)
            + "。" + escape(meaning["use_limit"]) + "</small>")


def render(record, evidence):
    request, result = record["request"], record["result"]
    decision = result["decision"]
    interim = record["schema_version"] in (INTERIM_SCHEMA, EXPLAINED_SCHEMA)
    coverage = "上半年合并口径：与上年同期比较，未年化" if interim else "年度财报范围"
    scripted = result["runtime_kind"] == "SCRIPTED_INTEGRATION"
    label = "合成演示 · 脚本化模型 · 非效果评价" if scripted else "投研草稿 · 待人工复核"
    body = (f"<span class='badge'>{label}</span><h1>{escape(request['source_id'])} · 盈利质量与现金转化研究</h1>"
        f"<p>{escape(request['question'])}</p><p class='muted'>资料截止 {escape(request['cutoff'])} · "
        f"研究期限 {request['horizon_months']} 个月 · {coverage}</p>"
        f"<p class='muted'>来源主体 CIK：{escape(request['entity_identifier'])}；请求中声明的代码：{escape(request['symbol'])}。本路线尚未独立核验代码与 CIK 映射，未连接行情或每股估值。</p>"
        f"<h2>本次判断：{escape(decision['outlook'])}</h2><p>{escape(decision['conclusion'])}</p>"
        "<p class='notice'>本报告研究经营假设及其变化。尚未覆盖完整估值与最新全部信息，不提供买卖评级、目标价或仓位。</p>"
        "<div class='flow'><span>事实审核</span><span>两份独立初稿</span><span>有限附注补查</span><span>综合判断</span><span>对照旧论点</span></div>")
    explanation = record.get("update_explanation")
    if explanation:
        prior = record["comparison"]["previous"]
        body += ("<h2>旧观点为什么维持或改变</h2><p>上次判断：" + escape(prior["outlook"])
            + "；本次判断：" + escape(decision["outlook"]) + "。</p><p>" + escape(explanation["proposal"]["summary"])
            + "</p><p class='muted'>以下说明在本次判断保存后生成，只解释变化，不修改当前结论；仍需人工核实解释是否得到来源支持。</p>")
        for item in explanation["proposal"]["items"]:
            old = prior["claims"][int(item["prior_claim_id"].split("-")[1]) - 1]
            current = [decision["claims"][int(ref.split("-")[1]) - 1]["statement"] for ref in item["current_claim_ids"]]
            body += ("<article><p><strong>" + escape(item["assessment"]) + "</strong> · 原论点：" + escape(old["statement"])
                + "</p><p>本次相关判断：" + escape("；".join(current) or "本次没有对应的已支持论点。")
                + "</p><p>变化理由：" + escape(item["reason"]) + " " + _refs(item["evidence_ids"])
                + "</p><p class='muted'>还需确认：" + escape(item["next_check"]) + "</p></article>")
    rows = []
    currency = evidence["analysis"]["currency"]
    unit = "CNY 亿元" if currency == "CNY" else currency + " 百万"
    for key, title in (("profit", "合并净利润"), ("operating_cashflow", "经营现金流")):
        metric = evidence["analysis"]["metrics"][key]
        rows.append(f"<tr><td>{title} {_refs(metric['fact_ids'])}</td><td>{_money(metric['comparison'], currency)}</td>"
                    f"<td>{_money(metric['current'], currency)}</td><td>{_money(metric['change'], currency)}</td></tr>")
    gap = evidence["analysis"]["metrics"]["cash_minus_profit"]
    rows.append(f"<tr><td>经营现金流 − 合并净利润</td><td>{_money(gap['comparison'],currency)}</td>"
                f"<td>{_money(gap['current'],currency)}</td><td>{_money(gap['change'],currency)}</td></tr>")
    body += ("<h2>关键事实</h2><p>单位：" + escape(unit) + "。数值由核验核心生成，模型不复写金额。</p>"
        "<table><tr><th>指标</th><th>比较期</th><th>本期</th><th>变化</th></tr>" + "".join(rows)
        + "</table><p><a href='financial-check.html'>查看完整财务核验、覆盖范围与缺项</a></p>")
    ratios = evidence["cash_profit_ratios"]
    if ratios:
        body += (f"<p>经营现金流 / 合并净利润：本期 {escape(ratios['current'])} 倍，比较期 {escape(ratios['comparison'])} 倍。"
                 "该比率由程序计算，不能单向解释为盈利质量评分。</p>")
    bridge = evidence["analysis"].get("profit_bridge")
    if bridge:
        body += "<h2>净利润为何变化：利润表核算</h2><p>同为上半年口径，金额由程序计算。下面的经营利润、非经营项目及税费变化相加解释净利润变化，不与现金流加回重复计算。</p><table><tr><th>利润表项目</th><th>对净利润同比变化的贡献（" + escape(unit) + "）</th></tr>"
        for item in bridge["components"]:
            body += f"<tr><td>{escape(item['label'])} {_refs(item['fact_ids'])}</td><td>{_money(item['change'],currency)}</td></tr>"
        body += f"<tr><td>净利润同比变化 {_refs(bridge['net_income']['fact_ids'])}</td><td>{_money(bridge['net_income']['change'],currency)}</td></tr></table><p class='muted'>这是报表项目的变动分解；投资损益净额不等同于单一公允价值或减值调节，分解不单独证明业务原因或未来持续性。</p>"
    body += "<h2>现金与利润差额：同比核算分解</h2><p>以下展示调节项目的同比变化，不将会计加回当作真实收款原因。</p><table><tr><th>项目</th><th>同比变化（" + escape(unit) + "）</th></tr>"
    for driver in evidence["analysis"]["drivers"][:6]:
        body += f"<tr><td>{escape(driver['label'])} {_refs(driver['fact_ids'])}</td><td>{_money(driver['change'],currency)}</td></tr>"
    body += "</table>"
    if interim:
        supplemental = evidence["analysis"].get("supplemental", {})
        body += "<h2>现金税与合同负债余额</h2><p>半年现金收付、同期税费与期末余额分别列示；余额差不等于现金流调节，税款与费用比较不等于完整税务勾稽。</p><table><tr><th>项目与期间</th><th>比较值</th><th>本期值</th></tr>"
        for key, title in (("cash_income_taxes_paid", "半年实付所得税净额"), ("current_tax_expense", "半年当期所得税费用"),
                           ("deferred_tax_expense", "半年递延所得税费用"), ("total_tax_expense", "半年所得税费用合计（程序求和）"),
                           ("contract_liability_balance", "合同负债余额（上年末→本期末）")):
            item = supplemental.get(key)
            body += (f"<tr><td>{title} {_refs(item['fact_ids'])}</td><td>{_money(item['comparison'],currency)}</td><td>{_money(item['current'],currency)}</td></tr>" if item else f"<tr><td>{title}</td><td colspan='2'>缺少可比输入</td></tr>")
        body += "</table><p class='muted'>金额单位与关键事实表相同。未提供客户毛收款，不据余额差或净调节倒推。</p>"
    cited = {ref for claim in decision["claims"] for ref in claim["evidence_ids"]}
    cited.update(x["evidence_id"] for x in decision["counterevidence_response"])
    amounts = [x for x in evidence["disclosure_amounts"] if x["note_id"] in cited]
    if amounts:
        body += "<h2>引用披露中的金额</h2><p>程序只统一明确币种与量纲；归属、预测实现与因果解释仍须按原文判断。</p><table><tr><th>原文表达</th><th>统一金额（" + escape(unit) + "）</th><th>来源</th></tr>"
        for item in amounts:
            body += f"<tr><td>{escape(item['expression'])}</td><td>{_money(item['value'],currency)}</td><td>{_refs([item['note_id']])}</td></tr>"
        body += "</table>"
    body += "<h2>经营解释与未来假设</h2>" + _claims(decision["claims"])
    body += "<h2>重要材料与反证的处理</h2>" + "".join(
        f"<p>{_refs([x['evidence_id']])} <strong>{escape(x['treatment'])}</strong>：{escape(x['reason'])}</p>"
        + _meaning(evidence, x["evidence_id"])
        for x in decision["counterevidence_response"])
    body += "<h2>后续判断条件</h2><p>以下是应观察的证据条件，不是数值预测或交易指令。</p><table><tr><th>判断变化</th><th>观察条件</th><th>应核对的证据</th></tr>" + "".join(
        f"<tr><td>{escape(x['name'])}</td><td>{escape(x['condition'])}</td><td>{escape(x['evidence_to_check'])}</td></tr>"
        for x in decision["scenarios"]) + "</table>"
    comparison = record["comparison"]
    body += "<h2>相对上次的变化</h2><p>" + escape(comparison["note"]) + "</p>"
    if interim:
        update = comparison["source_update"]
        body += "<p>资料变化：" + {"INITIAL_SOURCE": "首次资料", "NEW_SOURCE": "已使用不同来源文件", "SAME_SOURCE_RERUN": "同一来源重跑，不算新资料更新"}[update["kind"]] + "。</p>"
        if update["availability_relation"] == "ALREADY_PUBLIC_AT_PREVIOUS_CUTOFF":
            body += "<p class='notice'>本资料在上次研究截止日前已公开。本次是在补齐资料覆盖，不是上次研究日之后新公告触发的更新。</p>"
        if update["previous"]:
            before, after = update["previous"], update["current"]
            body += "<p class='notice'>以下并排展示各自覆盖期间的证据。全年与半年不能直接拼接、年化或据此计算环比增速。</p><table><tr><th>各自期间的证据</th><th>上次资料</th><th>本次资料</th></tr>"
            for key, title in (("period_basis", "比较口径"), ("published_at", "来源披露日期"), ("current_end", "本期结束"), ("comparison_end", "比较期结束")):
                body += f"<tr><td>{title}</td><td>{escape(before[key])}</td><td>{escape(after[key])}</td></tr>"
            for key, title in (("profit_change", "各自口径的利润同比变化"), ("cashflow_change", "各自口径的经营现金流同比变化"), ("contract_adjustment_current", "各自本期的合同负债现金流调节")):
                body += f"<tr><td>{title}</td><td>{_money(before[key],before['currency'])} {escape(before['currency'])}</td><td>{_money(after[key],after['currency'])} {escape(after['currency'])}</td></tr>"
            body += "</table><p class='muted'>CNY 以亿元展示，其他币种以百万展示。表格只是证据对照，不能代替对旧假设是否成立的金融判断。</p>"
    if comparison["previous"] is not None:
        old = comparison["previous"]
        body += ("<p>上次判断：<strong>" + escape(old["outlook"]) + "</strong>；本次判断：<strong>"
            + escape(decision["outlook"]) + "</strong></p><details><summary>查看原结论与假设</summary><p>"
            + escape(old["conclusion"]) + "</p>" + "".join("<p>" + escape(x["assumption"]) + "</p>"
                for x in old["claims"]) + "</details>")
    for key, title in (("limitations", "尚未解决的问题"), ("next_steps", "后续研究动作")):
        body += f"<h2>{title}</h2><ul>" + "".join(f"<li>{escape(x)}</li>" for x in decision[key]) + "</ul>"
    body += "<details><summary>两份独立初稿</summary><p class='notice'>原始候选分析可能含错误；未作为最终判断的输入。保留供复核其补查选择和分歧。</p><h2>经营分析</h2><p>" + escape(result["analysis"]["summary"])
    body += "</p>" + _claims(result["analysis"]["claims"]) + "<h2>独立质疑</h2><p>" + escape(result["challenge"]["summary"])
    body += "</p>" + _claims(result["challenge"]["claims"]) + "</details>"
    body += ("<p class='muted'>引用校验确认来源存在；经营解释与反证处理仍为模型草稿，不等于因果关系已验证。"
             "本轮模型调用数：" + str(explanation["budget"]["calls"] if explanation else result["budget"]["calls"]) + "。</p>")
    financial, sources = render_financial(evidence)
    return {"workpaper.html": _page("投资论点研究", body), "financial-check.html": financial, "evidence.html": sources}
