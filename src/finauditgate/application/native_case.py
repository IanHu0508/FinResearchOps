"""Cases for the original native graph with directly supplied audit evidence."""

import json
import re
from uuid import uuid4

from finauditgate.adapters.native_contract import audit_packet, validate_execution, MODEL_NODES
from finauditgate.application.contracts import ApplicationError
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.research_evidence import read_record
from finauditgate.contracts import RunRef
from finauditgate.research import FundamentalEvidenceTask, NativeResearchView


SCHEMA = "finresearchops.native-audited-case/v1"
LIVE_SCHEMA = "finresearchops.native-audited-case/v2"
CURRENT_SCHEMA = "finresearchops.native-audited-case/v4"
JUDGMENT_SCHEMA = "finresearchops.native-audited-case/v5"
LIVE_SCHEMAS = (LIVE_SCHEMA,CURRENT_SCHEMA,JUDGMENT_SCHEMA)
MAX_BYTES = 48 * 1024 * 1024


def render(record, packet):
    if record['schema_version']==JUDGMENT_SCHEMA:
        result=record['result']
        parts=['# 原生投研：经校验的论点与经理裁决','',
            '投资吸引力尚无法判断。估值、预期收益和实际持仓输入不足；Hold 仅为原生框架兼容字段，不是投资评级或持仓建议。','',
            '本模式只核验有限类型的事实、假设及裁决，未对任意自然语言或投资成效作保证。未校验的原始草稿和提案保留在私有轨迹中，不进入本页结论。','',
            f"研究日：{record['request']['as_of']}；研究期限：{record['request']['horizon_months']} 个月。",'',
            '经理选择与核心裁决分开记录；未经支持的论点不可因模型选择采用而进入结论。', '']
        for key,title in (('fundamentals_report','基本面论点'),('market_report','行情论点'),
                          ('investment_plan','研究经理逐项裁决'),('trader_investment_plan','交易员可执行性检查'),
                          ('final_trade_decision','组合经理逐项裁决')):
            parts+=['## '+title,'',result['reports'][key],'']
        parts+=['## 复核依据','',f"财务核心：{record['core_run_id']}；证券行情核心：{record['market_run_id']}",
            f"原生图 {len(result['topology_before']['nodes'])} 节点、{len(result['topology_before']['edges'])} 边；以下核心记录均在重开时重放：",'']
        parts += ['- '+r['node']+'：'+r['run_id'] for r in result['judgment_reviews']]
        return '\n'.join(parts).encode()
    result = record["result"]
    live = record['schema_version'] in LIVE_SCHEMAS
    parts = ["# 原生 TradingAgents 审核材料贯通验证", "",
        "真实模型与数据 · 原生完整决策链 · 待人工复核" if live else "离线合成资料与模拟模型 · 保留原生决策节点 · 待人工复核", "",
        "本报告验证材料进入了实际节点和模型输入，不证明真实模型会遵循材料或投资判断正确。", "",
        f"研究问题：{record['request']['question']}",
        f"声明证券：{record['request']['symbol']}；日期：{record['request']['as_of']}；期限：{record['request']['horizon_months']} 个月。", "",
        "## 程序保留的审核结论与限制", "",
        f"核心结论：{packet['core_decision']}；来源分析：{packet['analysis_status']}。",
        f"核心记录：{record['core_run_id']}",
        "检查问题：" + (", ".join(packet["issues"]) or "在当前有限取证范围内未报告问题"), "",
        "以下限制独立于模型摘要保留，不能被原生评级覆盖：", ""]
    parts += ["- " + x for x in packet["limits"]]
    if record['schema_version'] == CURRENT_SCHEMA:
        parts += ['', '财务工具的供应商原始返回已保留供复核；模型实际接收的是官方资料核对范围。未核对的供应商估值、每股数据、其他期间数据不进入讨论。',
            '财务事实正确不代表解释和投资判断正确。下方各阶段文字均为模型草稿，须结合最终复核记录阅读。']
    if record['schema_version'] == CURRENT_SCHEMA:
        from decimal import Decimal
        a = packet['financial_evidence']['analysis']
        parent = (a.get('earnings_attribution') or {}).get('parent')
        parts += ['', '## 先核对财务口径','',
            '以下数值直接来自核心来源记录；各阶段模型文字不能替换这些口径。单位：'+a['currency']+'。',
            '', '| 项目 | 比较期 | 本期 |','|---|---:|---:|']
        for label, value in (('合并净利润（现金流勾稽口径）',a['metrics']['profit']),
                             ('归母净利润',parent),('经营现金流净额',a['metrics']['operating_cashflow'])):
            if value:
                parts.append(f"| {label} | {Decimal(value['comparison']):,f} | {Decimal(value['current']):,f} |")
        parts += ['', '少数股东等归属扣减的负号不表示少数股东亏损。现金流损益调节不是利润表收益；正向加回不是正收益，负向处置损益调整不能说成处置亏损。递延税费用与现金流递延税调节分别取数。',
            '合并营业利润增长不自动证明游戏分部质量提升；现金流/净利润上升也不单独证明现金质量改善。']
    if live:
        identity, market = packet['security_market']['identity'],packet['security_market']['market']
        parts += ['', '## 证券与行情依据','',
            f"{identity['issuer']}；{identity['symbol']}；{identity['exchange']}；每份 ADS 对应 {identity['ordinary_shares_per_ads']} 股普通股。",
            f"报价币种 {identity['quote_currency']}；财报币种 {identity['financial_currency']}。两者未进行估值或每股换算。",
            f"研究日 {market['as_of']}；最近交易行 {market['latest_session']}；收盘 {market['values']['Close']} {identity['quote_currency']}/ADS。",
            '行情基于 Yahoo Finance 的复权 OHLCV，指标由固定上游代码计算；交易日期来自记录的日历核对。',
            '',market['snapshot'],'']
    parts += ["", "## 实际输入覆盖", "", "| 原生节点 | 模型调用次数 | 同一审核材料进入实际模型消息 |", "|---|---:|---|"]
    for node in MODEL_NODES:
        count = sum(c["node"] == node for c in result["model_calls"])
        parts.append(f"| {node} | {count} | 已逐条核对完整内容 |")
    parts += ["", f"原生图含 {len(result['topology_before']['nodes'])} 个节点（含起止）与 {len(result['topology_before']['edges'])} 条边；增强前后相同。",
              (f"实际工具执行记录 {len(result['tool_calls'])} 条；原生市场快照调用与前置核对一致。" if live else
               f"实际工具执行记录 {len(result['tool_calls'])} 条，包含 get_verified_market_snapshot 的合成执行；这不证明真实行情或证券映射已验证。"), "",
              "## 原生完整研究结果", "", f"原生信号：{result['signal']}（{'模型提案' if live else '模拟输出'}，无交易或投资批准）", ""]
    for key, title in (("fundamentals_report", "基本面报告"), ("market_report", "技术报告"),
                       ("investment_plan", "研究经理计划"), ("trader_investment_plan", "交易员提案"),
                       ("final_trade_decision", "风险讨论后的组合经理判断")):
        parts += [f"### {title}", "", result["reports"][key], ""]
    return "\n".join(parts).encode()


def run(application, command):
    application._require_private_artifact_root()
    if application._researcher is None:
        raise ApplicationError("NATIVE_ADAPTER_REQUIRED")
    execution = application._application_root / "native-audit-executions" / uuid4().hex
    request = {"symbol": command.symbol, "as_of": command.filing.cutoff.isoformat(),
        "question": command.question, "horizon_months": command.horizon_months,
        "entity_identifier": str(int(command.filing.entity_identifier))}
    ref = None
    try:
        from finauditgate.cashflow import InterimCashflowTask
        outcome = application._gate.run(FundamentalEvidenceTask(command.filing,
            include_owner_earnings=command.market_task is not None and type(command.filing) is InterimCashflowTask))
        ref = outcome.run_ref
        if not outcome.report["analysis"]["metrics"]:
            raise ValueError("NATIVE_AUDIT_INPUT_UNAVAILABLE")
        market_ref, market_record = None, None
        if command.market_task is not None:
            market = application._gate.run(command.market_task)
            market_ref, market_record = market.run_ref, market.report
        packet = audit_packet(request, outcome.report, ref.run_id, market_record, market_ref.run_id if market_ref else None,
                              source_filtered=market_ref is not None,reasoning_revision=2)
        session=None
        if command.check_judgments:
            from finauditgate.application.native_judgment import JudgmentSession,add_catalog
            session=JudgmentSession(application._gate,ref,market_ref)
            packet=add_catalog(packet,session.catalog)
        result = (application._researcher.run_native(request,packet,execution,judgment=session) if session else
                  application._researcher.run_native(request,packet,execution))
        validate_execution(result, request, packet)
        if session:
            from finauditgate.application.native_judgment import validate
            validate(application,result,packet)
        record = {"schema_version": JUDGMENT_SCHEMA if session else CURRENT_SCHEMA if market_ref else SCHEMA, "request": request, "core_run_id": ref.run_id,
                  "result": result, "review_status": "AWAITING_REVIEW"}
        if session:record['judgment_catalog_run_id']=session.catalog.run_ref.run_id
        if market_ref:
            record['market_run_id']=market_ref.run_id
        raw = canonical_json_bytes(record)
        if len(raw) > MAX_BYTES:
            raise ValueError("NATIVE_CASE_SIZE_LIMIT")
        case_ref = "case-" + sha256_hex(raw)
        directory = application._application_root / "native-audited-cases" / case_ref
        write_once(directory / "case.json", raw)
        write_once(directory / "report.md", render(record, packet))
        return application.read_case(case_ref)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "NATIVE_AUDIT_EXECUTION_FAILED"
        write_once(execution / "failure.json", canonical_json_bytes({"schema_version": "finresearchops.native-audit-failure/v1",
            "request": request, "core_run_id": None if ref is None else ref.run_id, "error": code}))
        raise ApplicationError(code) from exc


def load(application, case_ref):
    directory = application._application_root / "native-audited-cases" / case_ref
    try:
        raw = (directory / "case.json").read_bytes()
        record = json.loads(raw)
        live = record.get('schema_version') in LIVE_SCHEMAS
        required = {"schema_version", "request", "core_run_id", "result", "review_status"} | ({'market_run_id'} if live else set())
        if record.get('schema_version')==JUDGMENT_SCHEMA:required.add('judgment_catalog_run_id')
        if (len(raw) > MAX_BYTES or "case-" + sha256_hex(raw) != case_ref or canonical_json_bytes(record) != raw
                or set(record) != required
                or record["schema_version"] not in (SCHEMA,*LIVE_SCHEMAS) or record["review_status"] != "AWAITING_REVIEW"):
            raise ValueError("NATIVE_CASE_INVALID")
        ref = RunRef(record["core_run_id"])
        if not application._offline_gate.replay(ref).consistent:
            raise ValueError("NATIVE_CORE_REPLAY_FAILED")
        evidence = read_record(application._core_root, ref)
        request = record["request"]
        if (request["as_of"] != evidence["task"]["cutoff"]
                or request["entity_identifier"] != str(int(evidence["task"]["entity_identifier"]))):
            raise ValueError("NATIVE_REQUEST_SOURCE_MISMATCH")
        market_record = None
        if live:
            from finauditgate.core.security_market import read_record as read_market
            market_ref = RunRef(record['market_run_id'])
            if not application._offline_gate.replay(market_ref).consistent:
                raise ValueError('NATIVE_MARKET_REPLAY_FAILED')
            market_record = read_market(application._core_root, market_ref)
            if (market_record['identity']['symbol'] != request['symbol']
                    or market_record['identity']['entity_identifier'] != request['entity_identifier']
                    or market_record['market']['as_of'] != request['as_of']):
                raise ValueError('NATIVE_MARKET_REQUEST_MISMATCH')
        packet = audit_packet(request, evidence, ref.run_id, market_record, record.get('market_run_id'),
                              source_filtered=record['schema_version'] in (CURRENT_SCHEMA,JUDGMENT_SCHEMA),
                              reasoning_revision=2 if record['schema_version'] in (CURRENT_SCHEMA,JUDGMENT_SCHEMA) else 1)
        if record['schema_version']==JUDGMENT_SCHEMA:
            from types import SimpleNamespace
            from finauditgate.application.native_judgment import add_catalog,validate
            from finauditgate.core.judgment import read_record as read_judgment
            catalog_ref=RunRef(record['judgment_catalog_run_id'])
            if not application._offline_gate.replay(catalog_ref).consistent:raise ValueError('JUDGMENT_CATALOG_REPLAY_FAILED')
            catalog=read_judgment(application._core_root,catalog_ref)
            if catalog['task']['financial_ref']!=ref.run_id or catalog['task']['market_ref']!=record['market_run_id'] or catalog['task']['node']!='catalog':
                raise ValueError('JUDGMENT_CATALOG_BINDING_INVALID')
            packet=add_catalog(packet,SimpleNamespace(run_ref=catalog_ref,report=catalog))
            validate(application,record['result'],packet)
        validate_execution(record["result"], request, packet)
        if (directory / "report.md").read_bytes() != render(record, packet):
            raise ValueError("NATIVE_REPORT_CHANGED")
        return NativeResearchView(case_ref, "AWAITING_REVIEW", ref, str(directory / "report.md"), record)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ApplicationError("NATIVE_CASE_INTEGRITY_FAILED") from exc
