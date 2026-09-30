"""Formal research report: a readable layout of one saved thesis Case.

This is a presentation of the saved final research answer, not a second
analysis. The body uses only final-report prose that passes the same
numeric/source contract as the complete workpaper, effective program
calculations and source metadata. Intermediate stage outputs, complete source
excerpts and raw process records stay in report.md, process-record.md and
case.json. Rendering certifies no financial conclusion.
"""

import html
import json
import math
import re
import unicodedata

from .forward_report import _DISPOSITION, _EARNINGS_BASIS, _cell
from finauditgate.adapters.thesis_degrade import case_status
from finauditgate.adapters.thesis_protocol import payload_of
from .research_delivery import contract_for, evidence_catalog, report_context
from .research_narrative import _TOKEN, _escape, change_view
from .research_numbers import _INPUT_KEYS, _METRICS


V1 = "finresearchops.research-report/v1"
V2 = "finresearchops.research-report/v2"  # citation and unbound-number counts shown apart
VERSIONS = (V1, V2)
VERSION = V2  # written for newly saved Cases; saved files keep their recorded version
NUMBER_PENDING = "UNBOUND_RESEARCH_NUMBER_PENDING"
SUPPORTED = ("finresearchops.thesis-case/v16", "finresearchops.thesis-case/v17", "finresearchops.thesis-case/v18",
             "finresearchops.thesis-case/v19", "finresearchops.thesis-case/v20", "finresearchops.thesis-case/v21", "finresearchops.thesis-case/v22", "finresearchops.thesis-case/v23", "finresearchops.thesis-case/v24")
FILES = ("research-report.md", "research-report.html")

_RATINGS = {"Buy": "买入", "Overweight": "增持", "Hold": "中性", "Underweight": "减持", "Sell": "卖出",
            "REVIEW": "暂不评级"}
_EXCHANGES = {"SSE": "上海证券交易所", "SZSE": "深圳证券交易所", "BSE": "北京证券交易所"}
_MARKETS = {"CN_A": "A股"}
_SECTIONS = (("operating_performance", "一、经营表现与持续性"), ("earnings_quality", "二、盈利质量与归母勾稽"),
             ("cash_and_capital_allocation", "三、现金创造与资本配置"),
             ("valuation_and_price_requirements", "四、估值与当前价格要求"))
_STATUS = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}
_COVERAGE = {"available": "资料可用", "partial": "资料不完整", "unavailable": "无该类资料"}
_ANALYSTS = (("Fundamentals Analyst", "基本面分析"), ("Market Analyst", "市场与量价分析"),
             ("News Analyst", "新闻与事件分析"), ("Sentiment Analyst", "社媒与情绪分析"))
_BASIS = {"reported": "（披露）", "company_guidance": "（指引）", "reference_comparison": "（参考）"}
# Scenario parameters shown in the key-assumption table: (field, label, kind).
_ASSUMPTIONS = (
    ("revenue", "营业收入（{amount}）", "amount"),
    ("operating_margin", "经营利润率", "percent"),
    ("net_nonoperating_income", "非经营净收益（{amount}，损失为负）", "amount"),
    ("effective_tax_rate", "有效税率", "percent"),
    ("noncontrolling_attribution", "少数股东损益归属（{amount}）", "amount"),
    ("diluted_ordinary_shares", "摊薄普通股数（百万股）", "shares"),
    ("non_working_capital_adjustments", "非营运资金调整（{amount}）", "amount"),
    ("operating_asset_liability_cash_effect", "经营性资产负债现金影响（{amount}）", "amount"),
    ("cash_capex", "现金资本开支（{amount}）", "amount"),
    ("exit_pe", "期末条件市盈率（倍）", "multiple"),
    ("cash_dividend_per_traded_unit", "期间现金股息（{price}/{unit}）", "price"),
)
_MISSING = {"exit_pe": "期末市盈率", "cash_dividend_per_traded_unit": "期间现金股息", "market_price": "起点价格",
            "market_price_date": "起点行情日", "diluted_ordinary_shares": "摊薄普通股数",
            "fx_reporting_per_price_currency": "汇率", "shares_per_traded_unit": "每交易单位股数"}
# When the model's own clause does not name the scenario or the metric, the
# program appends them to the value, so a compact figure never loses its label.
_SHORT = {
    "revenue": "营业收入", "operating_margin": "经营利润率", "effective_tax_rate": "有效税率",
    "operating_profit": "经营利润", "pretax_income": "税前利润", "consolidated_net_income": "合并净利润",
    "noncontrolling_attribution_effect": "少数股东损益调节", "parent_net_income": "归母净利润",
    "eps_per_traded_unit": "每股收益", "operating_cash_flow": "经营现金流",
    "cash_after_capex_proxy": "扣资本开支现金流", "exit_price_per_traded_unit": "条件估值价格",
    "price_only_break_even_pe": "维持现价所需PE", "dividend_adjusted_break_even_pe": "含息打平PE",
    "return_ex_dividend": "不含息累计回报", "return_with_dividend": "含息累计回报",
}
_ALIASES = {
    "revenue": r"收入|营收", "operating_margin": r"利润率", "effective_tax_rate": r"税率",
    "operating_profit": r"经营利润(?!率)", "pretax_income": r"税前", "consolidated_net_income": r"合并净利",
    "noncontrolling_attribution_effect": r"少数股东", "parent_net_income": r"归母|母公司",
    "eps_per_traded_unit": r"每股|EPS", "operating_cash_flow": r"经营(?:活动)?现金流",
    "cash_after_capex_proxy": r"资本开支|资本购买|资本支出|代理",
    "exit_price_per_traded_unit": r"估值价格|条件价格", "price_only_break_even_pe": r"市盈率|PE",
    "dividend_adjusted_break_even_pe": r"市盈率|PE", "return_ex_dividend": r"回报",
    "return_with_dividend": r"回报",
}
# Break a long answer only where a sentence ends and a short label or an
# enumeration starts. Prose characters are never added, removed or reordered;
# a parenthesis holding nothing but citations is displayed as citation marks.
_BREAK = re.compile(r"(?<=。)(?=其[一二三四五六七八九十]+[，、]|[\u4e00-\u9fffA-Za-z0-9]{1,10}[：:](?!//))")
_LABEL = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{1,10}[：:](?!//)")
_SOURCE = r"\{\{source:[^{}:]+\}\}"
_PIECE = re.compile(r"[（(][ \t]*(" + _SOURCE + r"(?:[ \t]*[、，,；;][ \t]*" + _SOURCE + r")*)[ \t]*[）)]|" + _TOKEN.pattern)


def markdown_version(data):
    """The format version recorded on the first line of a saved Markdown report, or None."""
    match = re.match(rb"<!-- (finresearchops\.research-report/v[0-9]+) -->\n", data)
    return match.group(1).decode() if match else None


def html_version(data):
    """The format version recorded in a saved HTML report, or None."""
    match = re.search(rb'<meta name="generator" content="(finresearchops\.research-report/v[0-9]+)">', data)
    return match.group(1).decode() if match else None


class _Report:
    def __init__(self, record, version=VERSION):
        if record.get("schema_version") not in SUPPORTED:
            raise ValueError("RESEARCH_REPORT_CASE_VERSION_UNSUPPORTED")
        if version not in VERSIONS:
            raise ValueError("RESEARCH_REPORT_VERSION_UNSUPPORTED")
        self.version = version
        self.record = record
        self.draft = record["effective_forward_draft"]
        self.calculations = record["effective_forward_calculations"]
        # The same context as the workpaper: every prose field is checked
        # against the unchanged numeric, markup and source-binding contract.
        self.context = report_context(record["final_report"], self.draft, self.calculations,
            record["source_bundle"], record["request"], changes=change_view(record["applied_changes"]),
            beliefs=record["forward_revision"]["belief_updates"], contract=contract_for(record))
        check = self.context.evidence_check()
        self.degraded = record.get("recovery", {}).get("degraded", [])
        if record["evidence_check"] != check or record["status"] != case_status(self.degraded, check["status"], masked=bool(record.get("number_masking"))):
            raise ValueError("THESIS_EVIDENCE_CHECK_CHANGED")
        self.number_findings = [f for f in check["findings"] if f["reason"] == NUMBER_PENDING]
        self.citation_findings = [f for f in check["findings"] if f["reason"] != NUMBER_PENDING]
        self.scenarios = {s["scenario_id"]: s for s in self.draft["scenarios"]}
        self.results = {r["scenario_id"]: r for r in self.calculations["scenario_results"]}
        reporting = self.draft.get("reporting_currency") or ""
        price = self.draft.get("price_currency") or ""
        self.amount_unit = {"CNY": "百万元", "USD": "百万美元", "HKD": "百万港元"}.get(reporting, "百万" + reporting)
        self.price_unit = {"CNY": "元", "USD": "美元", "HKD": "港元"}.get(price, price)
        shares = (self.draft.get("shares_per_traded_unit") or {}).get("value")
        self.trade_unit = "股" if shares == 1 else "交易单位"
        self.citations = {}

    # Inline content -----------------------------------------------------

    def paragraphs(self, text, implied=None, field=None):
        self.context.text(text)  # identical validation to the workpaper
        parts, pending = [], ""
        for part in _BREAK.split(text):
            if not part.strip():
                continue
            if len(part.strip()) < 10:  # keep a very short sentence with the next one
                pending += part
                continue
            parts.append(pending + part)
            pending = ""
        if pending:
            parts.append(pending)
        return [self.runs(part, implied, field) for part in parts]

    def runs(self, text, implied=None, field=None):
        runs, end = [], 0
        for match in _PIECE.finditer(text):
            if match.start() > end:
                runs.append(("text", text[end:match.start()]))
            end = match.end()
            if match[1] is not None:
                runs += [self.source(qid) for qid in re.findall(r"\{\{source:([^{}:]+)\}\}", match[1])]
                continue
            token = match[2].split(":")
            if len(token) == 3 and token[0] == "metric":
                runs.append(self.metric(token[1], token[2], text[:match.start()], implied))
            elif len(token) == 2 and token[0] == "context" and token[1] in self.context.context:
                value = self.context.context[token[1]]
                runs.append(("ctx", "未提供" if value is None else str(value)))
            elif len(token) == 2 and token[0] == "source":
                runs.append(self.source(token[1]))
            else:
                raise ValueError("RESEARCH_NARRATIVE_REFERENCE_INVALID")
        if end < len(text):
            runs.append(("text", text[end:]))
        runs = [piece for run in runs for piece in (self.flags(run[1], field) if run[0] == "text" else [run])]
        if runs and runs[0][0] == "text":
            label = _LABEL.match(runs[0][1])
            if label:
                rest = runs[0][1][label.end():]
                runs[:1] = [("strong", label[0])] + ([("text", rest)] if rest else [])
        return runs

    def flags(self, text, field):
        """Mark the numerals recorded as unbound for this field during validation."""
        tokens = [f["reference"] for f in self.record["evidence_check"]["findings"]
                  if f["field"] == field and f["reason"] == "UNBOUND_RESEARCH_NUMBER_PENDING"]
        if not tokens:
            return [("text", text)]
        pattern = re.compile(r"(?<![A-Za-z0-9.])(?:" + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
                             + r")(?![A-Za-z0-9.])")
        pieces, end = [], 0
        for match in pattern.finditer(text):
            if match.start() > end:
                pieces.append(("text", text[end:match.start()]))
            pieces.append(("flag", match[0]))
            end = match.end()
        return pieces + ([("text", text[end:])] if end < len(text) else [])

    def metric(self, sid, key, before, implied):
        name, kind = _METRICS[key]
        value = self.scenarios[sid][key]["value"] if key in _INPUT_KEYS else self.results.get(sid, {}).get(key)
        clause = unicodedata.normalize("NFKC", _TOKEN.sub("", re.split(r"[。；;！？!?\n]", before)[-1]))
        labels = []
        if sid != implied and not re.search(r"(?<![A-Za-z0-9_])" + re.escape(sid) + r"(?![A-Za-z0-9_])", clause):
            labels.append(sid)
        if not re.search(_ALIASES[key], clause):
            labels.append(_SHORT[key])
        display = self.figure(value, kind) + ("（" + " ".join(labels) + "）" if labels else "")
        return ("value", display,
                f"{sid} · {name}；预测期间 {self.draft['forecast_start']} 至 {self.draft['forecast_end']}")

    def source(self, qid):
        if qid in self.context.quotes:
            return ("cite", self.citations.setdefault(qid, len(self.citations) + 1))
        return ("pending", qid)

    def figure(self, value, kind):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "未能计算"
        price = (" " + self.price_unit) if self.price_unit.isascii() else self.price_unit
        amount = self.amount_unit
        return {"percent": f"{value:.2%}", "return": f"{value:+.2%}（累计，非年化）",
                "multiple": f"{value:,.2f}倍", "price": f"{value:,.2f}{price}",
                "signed_amount": f"{value:+,.2f}{amount}"}.get(kind, f"{value:,.2f}{amount}")

    @staticmethod
    def plain(value, kind, missing="未能计算"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return missing
        return {"percent": f"{value:.2%}", "return": f"{value:+.2%}", "fx": f"{value:,.4f}"}.get(kind, f"{value:,.2f}")

    def scenario_name(self, sid):
        name = self.scenarios[sid].get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or "\n" in name:
            return None
        try:
            if self.context.unbound_numbers(name):  # same number and markup contract
                return None
        except ValueError:
            return None
        return name.strip()

    def parameter(self, field):
        """One label and number kind per input across every table of the report."""
        for key, label, kind in _ASSUMPTIONS:
            if key == field:
                return label.format(amount=self.amount_unit, price=self.price_unit, unit=self.trade_unit), kind
        draft = self.draft
        return {"market_price": (f"起点价格（{self.price_unit}/{self.trade_unit}）", "price"),
                "shares_per_traded_unit": ("每交易单位普通股数", "amount"),
                "fx_reporting_per_price_currency": (
                    f"汇率（{draft.get('reporting_currency')}/{draft.get('price_currency')}）", "fx")}[field]

    def assumption(self, field, value, kind):
        if field == "noncontrolling_attribution":
            amount = value.get("amount") or {}
            text = self.plain(amount.get("value"), kind, "未设定")
            if text == "未设定":
                return text
            prefix = {"profit": "扣除 ", "loss": "加回 "}.get(value.get("nature"), "性质未知 ")
            return prefix + text + _BASIS.get(amount.get("basis_type"), "")
        text = self.plain(value.get("value"), kind, "未设定")
        return text if text == "未设定" else text + _BASIS.get(value.get("basis_type"), "")

    # Document ------------------------------------------------------------

    def build(self):
        record, final = self.record, self.record["final_report"]
        request, draft = record["request"], self.draft
        identity = record["source_bundle"].get("identity") or {}
        name = identity.get("company_short_name")
        title = f"{name}（{request['symbol']}）公司研究报告" if name else f"{request['symbol']} 公司研究报告"
        venue = _EXCHANGES.get(identity.get("exchange"), identity.get("exchange") or "") + _MARKETS.get(identity.get("market"), "")
        subtitle = " ｜ ".join(x for x in (f"研究截止日 {request['as_of']}", f"研究期限 {request['horizon_months']}个月", venue) if x)
        blocks = [("title", title, subtitle)]
        if record["evidence_check"]["status"] != "COMPLETED":
            blocks.append(("notice", self.notice()))
        if self.degraded:
            names = "、".join(dict(_ANALYSTS).get(row["node"], "交易员") for row in self.degraded)
            blocks.append(("notice", f"本次{names}的输出未通过程序校验，已按降级规则省略，报告状态为部分完成；"
                                     "省略不代表资料中没有相关信息，其余研究阶段照常完成，详见附录一。"))
        if record.get("number_masking"):
            count, whole = masking_counts(record)
            blocks.append(("notice", f"终稿有{count}个句子含未能由程序核对的数字，这些数字已替换为“〔数值待核〕”"
                                     + (f"（其中{whole}句有文字无法逐个隐去数字，已整体隐去）" if whole else "") + "，报告状态为部分完成；"
                                     "评级、情景复算和其余文字未变，原句保留在过程记录中待人工核对。"))
        blocks.append(("lead", self.facts(), self.paragraphs(final["summary"]["text"], field="summary")))
        blocks += self.forecast_table()
        if "rule_rating" in record:
            from .research_conclusion import RULE_NOTE
            blocks.append(("note", RULE_NOTE))
        for key, heading in _SECTIONS:
            blocks.append(("h2", heading))
            blocks += [("p", runs) for runs in self.paragraphs(final["financial_analysis"][key]["text"],
                                                               field="financial_analysis." + key)]
        blocks += self.scenario_sections()
        blocks.append(("h2", "六、主要风险与反证"))
        blocks += [("p", runs) for runs in self.paragraphs(final["strongest_counterevidence"]["text"],
                                                           field="strongest_counterevidence")]
        blocks.append(("h2", "七、研究局限与待验证事项"))
        blocks.append(("ul", [[r for part in self.paragraphs(text, field=f"limitations[{i}]") for r in part]
                              for i, text in enumerate(final["limitations"])]))
        blocks.append(("appendix",))
        blocks += self.process_appendix()
        blocks += self.source_appendix()
        blocks.append(("disclaimer", self.disclaimer()))
        return title, blocks

    def notice(self):
        if self.version == V1:
            return "部分引用或数字未能由程序绑定到原文或计算，模型评级不能视为获准结论；待核项见附录二。"
        parts = ([f"{len(self.citation_findings)}项引用未能定位到所给资料原文"] if self.citation_findings else []) + \
                ([f"{len(self.number_findings)}个数字未经程序计算或来源绑定"] if self.number_findings else [])
        return "本报告" + "、".join(parts) + "，模型评级不能视为获准结论；待核项见附录二。"

    def binding_rows(self, check):
        if self.version == V1:
            return [("引用定位", f"{len(check['bindings'])}个证据块均定位到原文，不等于事实核验" if check["status"] == "COMPLETED"
                     else f"{len(check['findings'])}项待核，见附录二")]
        bound = len(check["bindings"])
        if self.citation_findings:
            located = f"{bound}个证据块定位到原文，{len(self.citation_findings)}项引用待核，见附录二"
        elif bound:
            located = f"{bound}个证据块均定位到原文，不等于事实核验"
        else:
            located = "未使用证据块引用"
        numbers = f"{len(self.number_findings)}项待核，见附录二" if self.number_findings else "无"
        return [("引用定位", located), ("未绑定数字", numbers)]

    def facts(self):
        record, draft = self.record, self.draft
        rating = record["final_report"]["rating"]
        price = (draft.get("market_price") or {}).get("value")
        start = "未提供" if price is None else self.figure(price, "price") + (
            f"（{draft['market_price_date']}）" if draft.get("market_price_date") else "")
        sources = record["source_bundle"]["sources"]
        research = sum(1 for s in sources if s.get("use", "research") == "research")
        scenario_notes = len(sources) - research
        mode = {"FROZEN_SOURCES": "冻结资料", "LIVE_VENDOR": "供应商工具资料"}.get(record["request"]["data_mode"], record["request"]["data_mode"])
        check = record["evidence_check"]
        comparison = record["rating_comparison"]
        rows = [("研究截止日", record["request"]["as_of"]),
                ("预测期间", f"{draft['forecast_start']} 至 {draft['forecast_end']}"),
                ("起点价格", start),
                ("独立初判→终稿", f"{comparison['before']} → {comparison['after']}"),
                ("研究资料", f"{mode}，{research}项" + (f"（另有{scenario_notes}项情景附录）" if scenario_notes else "")),
                *self.binding_rows(check),
                ("人工复核", "待复核，未签署")]
        if record.get("number_masking"):  # protocol 24: sentences whose refused numbers were hidden
            count, whole = masking_counts(record)
            rows.insert(-1, ("隐去数值", f"{count}个句子" + (f"（{whole}句有文字整体隐去）" if whole else "") + "，原句见过程记录"))
        if "rule_rating" in record:  # protocol 23: the conclusion's confidence and the rule beside it
            from .research_conclusion import CONFIDENCE, rule_text
            rows[:0] = [("置信度", CONFIDENCE[record["final_report"]["confidence"]]),
                        ("规则参考评级", rule_text(record["rule_rating"]))]
        return {"rating": rating, "rating_label": _RATINGS.get(rating, rating), "rows": rows}

    def forecast_table(self):
        draft, amount, price = self.draft, self.amount_unit, self.price_unit
        dispositions = {a["scenario_id"]: a["disposition"] for a in self.record["final_report"]["scenario_assessments"]}
        columns = [("revenue", f"营业收入（{amount}）", "amount", True), ("operating_margin", "经营利润率", "percent", True),
                   ("parent_net_income", f"归母净利润（{amount}）", "amount", False),
                   ("eps_per_traded_unit", f"EPS（{price}/{self.trade_unit}）", "price", False),
                   ("operating_cash_flow", f"经营现金流（{amount}）", "amount", False),
                   ("price_only_break_even_pe", "维持起点价格所需PE（倍）", "multiple", False)]
        for key, label, kind in (("exit_price_per_traded_unit", f"条件估值价格（{price}）", "price"),
                                 ("return_with_dividend", "含息累计回报", "return")):
            if any(self.plain(r.get(key), kind) != "未能计算" for r in self.results.values()):
                columns.append((key, label, kind, False))
        rows = []
        for scenario in draft["scenarios"]:
            sid = scenario["scenario_id"]
            row = [sid, _DISPOSITION.get(dispositions.get(sid), "未评估")]
            for key, _label, kind, is_input in columns:
                value = scenario[key]["value"] if is_input else self.results.get(sid, {}).get(key)
                row.append(self.plain(value, kind))
            rows.append(row)
        blocks = [("h2", "盈利预测与估值（情景）"),
                  ("table", ["情景", "终判采纳", *[c[1] for c in columns]], rows, set(range(2, 2 + len(columns))), {0, 1})]
        names = [(s["scenario_id"], self.scenario_name(s["scenario_id"])) for s in draft["scenarios"]]
        if any(n for _, n in names):
            blocks.append(("caption", "情景名称：" + "；".join(f"{sid} {n}" for sid, n in names if n) + "。"))
        basis = _EARNINGS_BASIS.get(draft.get("earnings_basis"), draft.get("earnings_basis") or "未说明")
        missing = []
        for result in self.results.values():
            for key in result.get("missing_inputs", []):
                label = _MISSING.get(key, key)
                if label not in missing:
                    missing.append(label)
        outputs = [label for key, label in (("exit_price_per_traded_unit", "条件估值价格"), ("return_ex_dividend", "不含息累计回报"),
                                            ("return_with_dividend", "含息累计回报"), ("dividend_adjusted_break_even_pe", "含息打平PE"))
                   if any(self.plain(r.get(key), "amount") == "未能计算" for r in self.results.values())]
        gaps = ("未设定" + "、".join(missing) + ("；" + "、".join(outputs) + "因此未计算。" if outputs else "。")) if missing else (
            "、".join(outputs) + "未计算。" if outputs else "")
        blocks.append(("note", f"预测期间 {draft['forecast_start']} 至 {draft['forecast_end']}，盈利口径为{basis}，"
                       f"条件估值日 {draft.get('valuation_date') or '未说明'}。维持起点价格所需PE＝起点价格÷情景EPS，"
                       "是给定盈利假设下的条件反推，不是公允倍数、目标估值或市场共识；经营现金流为情景推算，不等于股权自由现金流。" + gaps))
        return blocks

    def scenario_sections(self):
        blocks = [("h2", "五、情景分析")]
        for i, row in enumerate(self.record["final_report"]["scenario_assessments"]):
            sid = row["scenario_id"]
            name = self.scenario_name(sid)
            blocks.append(("h3", f"{sid}{' ' + name if name else ''}（{_DISPOSITION.get(row['disposition'], '未评估')}）"))
            blocks += [("p", runs) for runs in self.paragraphs(row["reason"], sid, f"scenario_assessments[{i}].reason")]
            blocks.append(("p", [("strong", "判断变化条件：")]))
            blocks += [("p", runs) for runs in self.paragraphs(row["what_changes_the_view"], sid,
                                                               f"scenario_assessments[{i}].what_changes_the_view")]
        draft = self.draft
        headers = ["参数", *[s["scenario_id"] for s in draft["scenarios"]]]
        rows = []
        fields = [field for field, _label, _kind in _ASSUMPTIONS]
        if (draft.get("reporting_currency") != draft.get("price_currency")
                or any((s.get("fx_reporting_per_price_currency") or {}).get("value") not in (None, 1) for s in draft["scenarios"])):
            fields.append("fx_reporting_per_price_currency")
        for field in fields:
            label, kind = self.parameter(field)
            rows.append([label, *[self.assumption(field, s.get(field) or {}, kind) for s in draft["scenarios"]]])
        shares = (draft.get("shares_per_traded_unit") or {}).get("value")
        if shares != 1:
            rows.append(["每交易单位普通股数", *[self.plain(shares, "amount", "未设定")] * len(draft["scenarios"])])
        blocks += [("h3", "关键假设（终稿采用的有效输入）"),
                   ("table", headers, rows, set(range(1, len(headers))), {0}),
                   ("note", "未标注者为模型提出的研究假设；标注“披露、指引、参考”的依据类型由模型声明，未经程序认证。"
                            "各参数的完整理由与来源编号见完整核对稿。")]
        return blocks

    def process_appendix(self):
        record = self.record
        final, comparison = record["final_report"], record["rating_comparison"]
        analysts = "analyst_reports" in record
        flow = (("基本面、市场、新闻与情绪四类分析 → " if analysts else "")
                + "多空研究员独立初稿与相互反证 → 研究经理 → 交易员 → 激进、保守、中性三类风险讨论 → "
                  "组合经理独立初判、前瞻推演、参数修正与终稿")
        left_out = f"（另有{len(self.degraded)}个非关键阶段按降级规则省略）" if self.degraded else ""
        blocks = [("h2", "附录一　研究流程与观点更新"),
                  ("p", [("text", f"本报告由{len(record['exchanges'])}个模型阶段依次产生{left_out}：{flow}。组合经理的独立初判评级为"
                          f"{comparison['before']}，终稿评级为{comparison['after']}（{'有变化' if comparison['changed'] else '未变化'}）。"
                          "各阶段原始输出、多空论点和逐处原文摘录见完整核对稿与过程记录。")])]
        if analysts:
            rows = [[title, "缺失（程序降级）", "—", "—"] if report.get("degraded") is True else
                    [title, _COVERAGE.get(report["coverage"], report["coverage"]), str(len(report["observations"])),
                     str(len(report["evidence_refs"]))]
                    for node, title in _ANALYSTS for report in [record["analyst_reports"][node]]]
            blocks += [("h3", "四类分析资料覆盖"), ("table", ["分析角色", "资料覆盖", "观察条数", "引用来源数"], rows, {2, 3}, {0, 1}),
                       ("note", "资料覆盖为各分析角色自报；四类分析全文是中间阶段输出，见完整核对稿。")]
        blocks.append(("h3", "独立观点的反证更新"))
        explanations = {r["belief_id"]: (i, r["explanation"]) for i, r in enumerate(final["belief_explanations"])}
        for update in record["forward_revision"]["belief_updates"]:
            bid = update["belief_id"]
            index, explanation = explanations[bid]
            blocks.append(("p", [("strong", f"{bid}（模型标记：{_STATUS.get(update['status'], update['status'])}）")]))
            blocks += [("p", runs) for runs in self.paragraphs(explanation["text"], field=f"belief_explanations[{index}].explanation")]
        blocks.append(("note", "维持或修改由模型自报，不证明判断质量改善；原独立观点全文见过程记录。"))
        blocks.append(("h3", "参数修改"))
        if record["applied_changes"]:
            rows, notes = [], {(r["scenario_id"], r["field"]): (i, r["explanation"]) for i, r in enumerate(final["change_explanations"])}
            for edit in record["applied_changes"]:
                field = edit["field"]
                label, kind = self.parameter(field)
                before, after = (self.assumption(field, edit[k], kind) for k in ("before", "after"))
                changed = _value(field, edit["before"]) != _value(field, edit["after"])
                rows.append([edit["scenario_id"] or "共同参数", label, before, after, "数值已改" if changed else "仅理由变化"])
            blocks.append(("table", ["情景", "参数", "修改前", "修改后", "程序核对"], rows, {2, 3}, {0, 1, 4}))
            for edit in record["applied_changes"]:
                note = notes.get((edit["scenario_id"], edit["field"]))
                if note is not None:
                    index, explanation = note
                    blocks += [("p", runs) for runs in self.paragraphs(explanation["text"], edit["scenario_id"],
                                                                       f"change_explanations[{index}].explanation")]
        else:
            blocks.append(("p", [("text", "参数修正阶段未改动情景参数；原假设沿用，不表示已获验证。")]))
        if record.get("number_repair"):
            count = len(record["number_repair"]["replacements"])
            blocks.append(("note", f"终稿有{count}个句子因含未绑定数字被拒收，已只改写这些句子；其余文字、评级和情景采纳未变，"
                                   "改写后整份终稿按同一数字规则重新检查。原句与改写句见过程记录。"))
        if record.get("number_masking"):
            count, whole = masking_counts(record)
            blocks.append(("note", f"终稿有{count}个句子因含未绑定数字被拒收，句子级修复无法进行或未能通过；"
                                   "程序把这些句子中数字规则不接受的数字替换为待核标记"
                                   + (f"（其中{whole}句有文字无法逐个替换，已整体隐去，引用保留）" if whole else "")
                                   + "，没有再调用模型，其余文字、评级和情景采纳未变。原句与处理后句子见过程记录。"))
        blocks += self.quant_note()
        blocks += self.user_inputs()
        return blocks

    def quant_note(self):
        record = self.record
        if not any(s["id"] == "QUANT" and s.get("use", "research") == "research" for s in record["source_bundle"]["sources"]):
            return []
        from .thesis_report_v17 import _quant_citations
        blocks_ids = {row["id"] for row in evidence_catalog(record["source_bundle"]) if row["source_id"] == "QUANT"}
        supplied = cited = 0
        for exchange in record["exchanges"]:
            payload = payload_of(exchange["messages"][1]["content"], int(record["schema_version"].rsplit("/v", 1)[1]))
            supplied += any(s.get("id") == "QUANT" for s in payload.get("source_bundle", {}).get("sources", []))
            cited += bool(_quant_citations(exchange["parsed"], blocks_ids))
        numbers = sorted(n for qid, n in self.citations.items() if self.context.quotes[qid]["source_id"] == "QUANT")
        return [("h3", "量化信号的使用"),
                ("p", [("text", f"量化研究说明（QUANT）作为研究资料进入{supplied}/{len(record['exchanges'])}个阶段，"
                        f"其中{cited}个阶段的返回显式引用；本报告引用编号："
                        + ("、".join(f"［{n}］" for n in numbers) if numbers else "无") + "。"
                        "该说明表示其所述期限内的横截面相对排序，不是上涨概率、收益预测或评级；引用不证明量化信号改善了判断。")])]

    def user_inputs(self):
        request = self.record["request"]
        blocks = [("h3", "研究问题与用户输入"), ("p", [("strong", "研究问题："), ("text", request["question"])])]
        for key, label in (("hypotheses", "待检验假设（非已验证事实）："), ("research_constraints", "研究约束：")):
            if request.get(key):
                blocks += [("p", [("strong", label)]), ("ul", [[("text", item)] for item in request[key]])]
        if request.get("user_view"):
            blocks.append(("p", [("strong", "用户期望（单独保存，不作为证据或评级指令）："), ("text", request["user_view"])]))
            blocks.append(("note", "读回时程序逐阶段核对研究请求，均不含该字段。"))
        return blocks

    def source_appendix(self):
        record = self.record
        blocks = [("h2", "附录二　引用与资料目录")]
        entries = []
        for qid, number in sorted(self.citations.items(), key=lambda item: item[1]):
            quote = self.context.quotes[qid]
            entries.append((number, qid, quote["source_id"], quote["start"], quote["end"], quote["quote"]))
        if entries:
            blocks += [("h3", "引用编号"), ("refs", entries),
                       ("note", "编号对应所给资料中由程序定位的原文片段，逐处摘录见完整核对稿；定位成功不证明事实或解释正确。")]
        findings = record["evidence_check"]["findings"]
        if findings:
            reasons = {"THESIS_UNKNOWN_EVIDENCE_BLOCK": "所选证据块不存在，需重新选择原文依据",
                       "THESIS_UNKNOWN_SOURCE_REFERENCE": "来源不存在或不在本次研究资料范围",
                       "RESEARCH_SOURCE_QUOTE_NOT_UNIQUE": "原文未找到唯一匹配，出处仍需核对",
                       "UNBOUND_RESEARCH_NUMBER_PENDING": "未绑定数字：未经程序计算或来源绑定，需核对是否为金融数值"}
            if self.version == V1:
                blocks += [("h3", "待核项"),
                           ("table", ["位置", "引用", "问题"], [[r["field"], r["reference"], reasons.get(r["reason"], "证据关联需要复核")]
                                                              for r in findings], set(), {1})]
            else:
                if self.citation_findings:
                    blocks += [("h3", "引用待核"),
                               ("table", ["位置", "引用", "问题"], [[r["field"], r["reference"], reasons.get(r["reason"], "证据关联需要复核")]
                                                                  for r in self.citation_findings], set(), {1})]
                if self.number_findings:
                    blocks += [("h3", "数字待核"),
                               ("table", ["位置", "数字", "说明"], [[r["field"], r["reference"], "未经程序计算或来源绑定，需核对是否为金融数值"]
                                                                  for r in self.number_findings], set(), {1})]
        rows = []
        for source in record["source_bundle"]["sources"]:
            origin = source["origin"] + ("（情景附录资料，未进入评级请求）" if source.get("use") == "sensitivity" else "")
            rows.append([source["id"], origin, source["availability_note"]])
        blocks += [("h3", "资料目录"), ("table", ["编号", "来源说明", "时间可得性与限制"], rows, set(), {0})]
        return blocks

    def disclaimer(self):
        mode = {"FROZEN_SOURCES": "冻结资料", "LIVE_VENDOR": "供应商工具资料"}.get(
            self.record["request"]["data_mode"], self.record["request"]["data_mode"])
        return ["本报告由 FinResearchOps 研究流程基于" + mode + "生成，评级为模型提出的研究结论，未经人工金融复核签署，不构成投资建议。",
                "正文预测数值来自本次有效参数与程序计算；带编号的引用已定位到所给资料原文，但引用吻合不证明事实、经济解释或预测正确。",
                "各资料的时间可得性与限制见资料目录；完整核对稿（report.md）保留逐处原文摘录与全部说明，过程记录（process-record.md）"
                "保留各阶段原始输出，结构化记录见 case.json。",
                "报告格式 " + self.version + "，由已保存的 Case 确定性生成。"]


def masking_counts(record):
    """(masked sentences, sentences hidden whole) of a protocol 24 Case."""
    rows = record["number_masking"]["sentences"]
    return len(rows), sum(r["mode"] == "sentence" for r in rows)


def _value(field, assumption):
    if field == "noncontrolling_attribution":
        return assumption.get("nature"), (assumption.get("amount") or {}).get("value")
    return assumption.get("value")


# Markdown -----------------------------------------------------------------

def _md_runs(runs):
    out = []
    for run in runs:
        if run[0] == "strong":
            out.append("**" + _escape(run[1]) + "**")
        elif run[0] == "cite":
            out.append(f"［{run[1]}］")
        elif run[0] == "pending":
            out.append("〔证据待核：" + _escape(run[1]) + "〕")
        elif run[0] == "flag":
            out.append(_escape(run[1]) + "〔待核〕")
        else:
            out.append(_escape(run[1]))
    return "".join(out)


def _md_table(headers, rows, numeric):
    align = ["---:" if i in numeric else "---" for i in range(len(headers))]
    return ["| " + " | ".join(_cell(h) for h in headers) + " |", "| " + " | ".join(align) + " |",
            *["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows], ""]


def _markdown(title, blocks, version):
    lines = ["<!-- " + version + " -->"]
    for block in blocks:
        kind = block[0]
        if kind == "title":
            lines += ["# " + _escape(block[1]), "", _escape(block[2]), ""]
        elif kind == "notice":
            lines += ["> **" + _escape(block[1]) + "**", ""]
        elif kind == "lead":
            facts = block[1]
            lines += _md_table(["模型评级", f"{facts['rating']}（{facts['rating_label']}）"], facts["rows"], set())
            lines += ["## 投资要点", ""]
            for runs in block[2]:
                lines += [_md_runs(runs), ""]
        elif kind == "h2":
            lines += ["## " + _escape(block[1]), ""]
        elif kind == "h3":
            lines += ["### " + _escape(block[1]), ""]
        elif kind == "p":
            lines += [_md_runs(block[1]), ""]
        elif kind == "ul":
            lines += ["- " + _md_runs(runs) for runs in block[1]] + [""]
        elif kind == "table":
            lines += _md_table(block[1], block[2], block[3])
        elif kind == "note":
            lines += ["注：" + _escape(block[1]), ""]
        elif kind == "caption":
            lines += [_escape(block[1]), ""]
        elif kind == "refs":
            lines += _md_table(["编号", "来源", "证据块", "原文位置（字符）"],
                               [[f"［{n}］", sid, qid, f"{start:,}–{end:,}"] for n, qid, sid, start, end, _ in block[1]], set())
        elif kind == "appendix":
            lines += ["---", ""]
        elif kind == "disclaimer":
            lines += ["## 重要说明", ""] + ["- " + _escape(item) for item in block[1]] + [""]
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


# HTML -----------------------------------------------------------------------

_CSS = """
:root{--ink:#1c2128;--muted:#5f6b7a;--line:#dde1e7;--accent:#1f3a5f;--soft:#eef2f7;--warn:#8a4b00;--paper:#fff}
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;color:var(--ink);font:15px/1.8 "PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.report{max-width:920px;margin:32px auto;background:var(--paper);padding:44px 56px 40px;box-shadow:0 1px 4px rgba(15,23,42,.08)}
.masthead{border-bottom:3px solid var(--accent);padding-bottom:14px;margin-bottom:22px}
.brand{font-size:12px;letter-spacing:.14em;color:var(--accent);font-weight:600}
h1{font-size:26px;line-height:1.35;margin:6px 0 4px;letter-spacing:.02em}
.subtitle{color:var(--muted);margin:0;font-size:13px}
.notice{border-left:4px solid var(--warn);background:#fff7ec;color:var(--warn);padding:10px 14px;margin:0 0 18px;font-weight:600}
.lead{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;align-items:start}
.lead h2{margin-top:0}
.facts{background:var(--soft);border-top:3px solid var(--accent);padding:14px 16px;font-size:13px}
.rating{border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:8px}
.rating-label{color:var(--muted);font-size:12px}
.rating-value{font-size:24px;font-weight:700;color:var(--accent);line-height:1.3}
.rating-note{font-size:13px;color:var(--ink)}
.facts dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 10px}
.facts dt{color:var(--muted);white-space:nowrap}
.facts dd{margin:0;overflow-wrap:break-word}
.date{white-space:nowrap}
h2{font-size:18px;color:var(--accent);border-left:4px solid var(--accent);padding-left:10px;margin:34px 0 12px;line-height:1.4}
h3{font-size:15.5px;margin:22px 0 8px;color:var(--ink)}
p{margin:0 0 10px;text-align:justify}
ul{margin:0 0 12px;padding-left:1.4em}
li{margin:0 0 6px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 8px}
th{background:var(--accent);color:#fff;font-weight:600;padding:6px 8px;text-align:left;vertical-align:bottom}
td{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top;overflow-wrap:anywhere}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
td.num{white-space:nowrap}
th.key,td.key{white-space:nowrap}
tbody tr:nth-child(even){background:#f8f9fb}
.note{font-size:12.5px;color:var(--muted);margin:0 0 10px}
.caption{font-size:13px;margin:2px 0 6px}
sup.cite{font-size:10.5px;line-height:0;margin:0 1px}
sup.cite a{color:var(--accent);text-decoration:none}
.figure{border-bottom:1px dotted #9aa4b2}
.pending{color:var(--warn);font-weight:600}
.appendix{margin-top:40px;border-top:1px solid var(--line);font-size:13.5px}
.appendix h2{font-size:16.5px}
.refs{list-style:none;padding:0;margin:0 0 10px}
.refs li{margin:0 0 4px}
.ref-no{display:inline-block;min-width:2.6em;color:var(--accent);font-weight:600}
details{margin:2px 0 6px 2.6em}
summary{cursor:pointer;color:var(--muted);font-size:12.5px}
details pre{white-space:pre-wrap;word-break:break-word;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f6f7f9;border:1px solid var(--line);padding:8px 10px;max-height:340px;overflow:auto}
.disclaimer{margin-top:36px;border-top:1px solid var(--line);padding-top:10px;font-size:12px;color:var(--muted)}
.disclaimer h2{font-size:14px;color:var(--muted);border-left-color:var(--line)}
@media (max-width:760px){.report{margin:0;padding:24px 18px}.lead{grid-template-columns:1fr}}
@media print{@page{size:A4;margin:16mm 14mm}body{background:#fff}.report{box-shadow:none;margin:0;max-width:none;padding:0}h2,h3{break-after:avoid}tr{break-inside:avoid}details{display:none}}
"""


def _h(text):
    return html.escape(str(text), quote=False)


def _dates(escaped):
    return re.sub(r"(?<![0-9-])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9-])", r'<span class="date">\1</span>', escaped)


def _html_runs(runs):
    out = []
    for run in runs:
        if run[0] == "strong":
            out.append("<strong>" + _h(run[1]) + "</strong>")
        elif run[0] == "cite":
            out.append(f'<sup class="cite"><a href="#ref-{run[1]}">[{run[1]}]</a></sup>')
        elif run[0] == "pending":
            out.append('<span class="pending">〔证据待核：' + _h(run[1]) + "〕</span>")
        elif run[0] == "flag":
            # Inline style keeps reports without flags byte-identical to v1.
            out.append('<span class="flag" style="background:#fff3d6;border-bottom:1px solid #c98a00" '
                       'title="未经程序计算或来源绑定的数字，需核对">' + _h(run[1]) + "</span>")
        elif run[0] == "value":
            out.append('<span class="figure" title="' + html.escape(run[2]) + '">' + _h(run[1]) + "</span>")
        else:
            out.append(_h(run[1]))
    return "".join(out)


def _html_table(headers, rows, numeric, keys):
    def cell(tag, index, value):
        attribute = ' class="num"' if index in numeric else ' class="key"' if index in keys else ""
        return f"<{tag}{attribute}>{_h(value)}</{tag}>"

    head = "".join(cell("th", i, h) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, v) for i, v in enumerate(row)) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _html(title, blocks, version):
    parts, appendix = [], False
    for block in blocks:
        kind = block[0]
        if kind == "title":
            parts.append('<header class="masthead"><div class="brand">FINRESEARCHOPS · 公司研究</div>'
                         f"<h1>{_h(block[1])}</h1><p class=\"subtitle\">{_h(block[2])}</p></header>")
        elif kind == "notice":
            parts.append(f'<p class="notice">{_h(block[1])}</p>')
        elif kind == "lead":
            facts = block[1]
            rows = "".join(f"<dt>{_h(k)}</dt><dd>{_dates(_h(v))}</dd>" for k, v in facts["rows"])
            summary = "".join(f"<p>{_html_runs(runs)}</p>" for runs in block[2])
            parts.append('<section class="lead"><div class="thesis"><h2>投资要点</h2>' + summary + "</div>"
                         '<aside class="facts"><div class="rating"><div class="rating-label">模型评级</div>'
                         f'<div class="rating-value">{_h(facts["rating"])}</div><div class="rating-note">{_h(facts["rating_label"])}</div></div>'
                         f"<dl>{rows}</dl></aside></section>")
        elif kind == "h2":
            parts.append(f"<h2>{_h(block[1])}</h2>")
        elif kind == "h3":
            parts.append(f"<h3>{_h(block[1])}</h3>")
        elif kind == "p":
            parts.append(f"<p>{_html_runs(block[1])}</p>")
        elif kind == "ul":
            parts.append("<ul>" + "".join(f"<li>{_html_runs(runs)}</li>" for runs in block[1]) + "</ul>")
        elif kind == "table":
            parts.append(_html_table(block[1], block[2], block[3], block[4]))
        elif kind == "note":
            parts.append(f'<p class="note">注：{_h(block[1])}</p>')
        elif kind == "caption":
            parts.append(f'<p class="caption">{_h(block[1])}</p>')
        elif kind == "refs":
            items = "".join(f'<li id="ref-{n}"><span class="ref-no">[{n}]</span>{_h(sid)} · 证据块 {_h(qid)} · 原文字符 {start:,}–{end:,}'
                            f"<details><summary>展开原文片段</summary><pre>{_h(text)}</pre></details></li>"
                            for n, qid, sid, start, end, text in block[1])
            parts.append(f'<ol class="refs">{items}</ol>')
        elif kind == "appendix":
            parts.append('<div class="appendix">')
            appendix = True
        elif kind == "disclaimer":
            if appendix:
                parts.append("</div>")
                appendix = False
            parts.append('<footer class="disclaimer"><h2>重要说明</h2><ul>'
                         + "".join(f"<li>{_h(item)}</li>" for item in block[1]) + "</ul></footer>")
    if appendix:
        parts.append("</div>")
    return ("<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<meta name=\"generator\" content=\"{version}\">\n<title>{_h(title)}</title>\n"
            f"<style>{_CSS}</style>\n</head>\n<body>\n<main class=\"report\">\n" + "\n".join(parts)
            + "\n</main>\n</body>\n</html>\n").encode("utf-8")


def render(record, version=VERSION):
    """Return (Markdown bytes, HTML bytes) for a v16-v24 thesis Case in one format version."""
    report = _Report(record, version)
    title, blocks = report.build()
    return _markdown(title, blocks, version), _html(title, blocks, version)
