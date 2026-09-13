"""Resolve v13 narrative references; never certify an economic interpretation.

Forecast amounts come from effective calculations. Source quotations are exact,
uniquely located excerpts, explicitly displayed as source text rather than as
effective forecasts. Unbound numerical literals cannot silently enter prose.
"""

from copy import deepcopy
import html
import re
import unicodedata

from .research_numbers import _INPUT_KEYS, _METRICS, _number, render_research_block


NARRATIVE_INSTRUCTION = (
    "正文采用数值引用契约：不得直接输入任何阿拉伯数字、金额、比率、中文数额或英文拼写数额。"
    "前瞻数值写{{metric:F1:eps_per_traded_unit}}这种引用，程序插入完整指标名、有效值、单位和期间；"
    "可用metric键见metrics的枚举，未知值会显示未能计算。F1等实际情景编号和S01等来源编号可以直接写。"
    "日期/代码用{{context:as_of}}、{{context:symbol}}、{{context:forecast_start}}、"
    "{{context:forecast_end}}、{{context:valuation_date}}、{{context:market_price_date}}或{{context:horizon_months}}。"
    "历史数量必须放在source_quotes中：每条为{id:Q1,source_id:S01,quote:逐字原文}；"
    "quote必须在该来源中恰好出现一次，保留完整句子或表头、期间、单位和行名上下文，不另填或改写数值。"
    "正文用{{source:Q1}}引用，程序明确展示为来源摘录而不是本次预测，引用吻合不代表经济解释获验证。"
    "source_quotes没有需要时为[]。不用HTML、Markdown链接、代码块或自行创造引用语法绕过此契约。"
    "该契约只用于最终报告的全部解释，包括reason、what_changes_the_view、limitations及explanation.text。"
    "change_explanations必须且仅覆盖change_context中的每个scenario_id与field组合，"
    "belief_explanations必须且仅覆盖research_resolution.belief_updates中的每个belief_id。"
    "它们只解释已记录的参数变化和信念更新，不重作修正、不改变更新状态。"
    "明确区分已发现的历史/会计错误和未来研究假设，不能把数学复算当作来源或金额获证实。"

)

_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
_CN_AMOUNT = re.compile(
    r"(?:百分之|千分之)[零〇一二两三四五六七八九十百千万亿点]+|"
    r"[零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖][零〇一二两三四五六七八九十百千万亿兆点壹贰叁肆伍陆柒捌玖拾佰仟]{1,}|"
    r"[零〇一二两三四五六七八九十百千万亿](?:倍|元|美元|港元|人民币|成)(?!本)"
)
_EN_AMOUNT = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                        r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
                        r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|trillion)\b", re.I)
_CN_FINANCIAL_NUMBER = re.compile(r"(?:EPS|每股收益|每股盈利|收入|利润率?|回报|股价|目标价|金额|股息|市盈率)"
                                  r"[^。；\n]{0,8}[零〇一二两三四五六七八九十百千万亿]+(?:[。；，,\s]|$)", re.I)


def _escape(text):
    # Source text must not become HTML, a link, or a second interpretation pass.
    text = html.escape(str(text), quote=False)
    return re.sub(r"([\\`*_\[\]{}|])", r"\\\1", text).replace("\n", " ")


def _locate_quote(body, quote):
    start = body.find(quote)
    if start >= 0:
        if body.find(quote, start + 1) >= 0:
            raise ValueError("RESEARCH_SOURCE_QUOTE_NOT_UNIQUE")
        return start, start + len(quote), "exact"
    # PDF/HTML line wrapping is not a factual edit. Only collapse whitespace
    # runs, keep token boundaries, then map back to the untouched source bytes'
    # decoded character offsets. Never normalize digits or punctuation.
    chars, starts, ends = [], [], []
    for match in re.finditer(r"\s+|\S", body):
        chars.append(" " if match[0].isspace() else match[0])
        starts.append(match.start())
        ends.append(match.end())
    normalized = "".join(chars)
    query = re.sub(r"\s+", " ", quote).strip()
    start = normalized.find(query)
    if not query or start < 0 or normalized.find(query, start + 1) >= 0:
        raise ValueError("RESEARCH_SOURCE_QUOTE_NOT_UNIQUE")
    return starts[start], ends[start + len(query) - 1], "whitespace_normalized"


class NarrativeContext:
    def __init__(self, draft, calculations, sources, quotes, request):
        self.draft, self.calculations = draft, calculations
        self.scenarios = {s["scenario_id"]: s for s in draft["scenarios"]}
        self.results = {s["scenario_id"]: s for s in calculations["scenario_results"]}
        self.sources = {s["id"]: s for s in sources["sources"] if s.get("use", "research") == "research"}
        self.context = {k: draft.get(k) for k in ("forecast_start", "forecast_end", "valuation_date", "market_price_date")}
        self.context.update({k: request[k] for k in ("symbol", "as_of", "horizon_months")})
        self.quotes = {}
        if not isinstance(quotes, list) or len(quotes) > 12:
            raise ValueError("RESEARCH_SOURCE_QUOTES_INVALID")
        for quote in quotes:
            if (not isinstance(quote, dict) or set(quote) != {"id", "source_id", "quote"}
                    or not isinstance(quote["id"], str) or not re.fullmatch(r"Q[1-9][0-9]?", quote["id"])
                    or quote["id"] in self.quotes or not isinstance(quote["source_id"], str) or quote["source_id"] not in self.sources
                    or not isinstance(quote["quote"], str) or not 12 <= len(quote["quote"]) <= 1200):
                raise ValueError("RESEARCH_SOURCE_QUOTES_INVALID")
            source = self.sources[quote["source_id"]]
            body = source["content"]
            # Ambiguous snippets cannot acquire a fabricated locator.
            start, end, matching = _locate_quote(body, quote["quote"])
            self.quotes[quote["id"]] = {**quote, "quote": body[start:end], "start": start, "end": end, "matching": matching}
        identifiers = sorted(set(self.scenarios) | set(self.sources) | {"D1", "D2", "D3", "D4", "B1", "B2", "B3", "B4", "S1", "S2", "S3", "S4"}, key=len, reverse=True)
        self.identifiers = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(i) for i in identifiers) + r")(?![A-Za-z0-9_])")

    def _literal(self, text):
        clean = unicodedata.normalize("NFKC", text)
        clean = self.identifiers.sub("", clean)
        original = self.identifiers.sub("", text)
        if (any(c.isnumeric() for c in clean + original if not ('\u4e00' <= c <= '\u9fff'))
                or _CN_AMOUNT.search(clean) or _EN_AMOUNT.search(clean) or _CN_FINANCIAL_NUMBER.search(clean)):
            raise ValueError("UNBOUND_RESEARCH_NUMBER")
        if any(c in clean for c in ("{", "}", "<", ">", "`", "[", "]", "\\")):
            raise ValueError("RESEARCH_NARRATIVE_MARKUP_INVALID")
        return _escape(text)

    def metric(self, sid, key):
        # Keep validation and metric names identical to the existing table seam.
        render_research_block({"text": "", "metrics": [{"scenario_id": sid, "metric": key}], "evidence_refs": []}, self.draft, self.calculations)
        name, kind = _METRICS[key]
        value = self.scenarios[sid][key]["value"] if key in _INPUT_KEYS else self.results.get(sid, {}).get(key)
        number = _number(value, percent=kind in {"percent", "return"}, signed=kind in {"signed_amount", "return"}, precise=True)
        unit = {"amount": f"百万元 {self.draft['reporting_currency']}", "signed_amount": f"百万元 {self.draft['reporting_currency']}",
                "price": f"{self.draft['price_currency']}／交易单位", "multiple": "倍", "percent": "", "return": "（累计，非年化）"}[kind]
        period = f"{self.draft['forecast_start']} 至 {self.draft['forecast_end']}"
        return _escape(f"〔{sid} · {name}：{number} {unit}；预测期间 {period}〕")

    def text(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("RESEARCH_NARRATIVE_TEXT_INVALID")
        parts, end = [], 0
        for match in _TOKEN.finditer(text):
            parts.append(self._literal(text[end:match.start()]))
            token = match[1].split(":")
            if len(token) == 3 and token[0] == "metric":
                parts.append(self.metric(token[1], token[2]))
            elif len(token) == 2 and token[0] == "context" and token[1] in self.context:
                labels = {"symbol": "证券代码", "as_of": "研究截止日", "horizon_months": "研究期限（月）",
                          "forecast_start": "预测期开始", "forecast_end": "预测期结束",
                          "valuation_date": "条件估值日", "market_price_date": "起点行情日"}
                parts.append(_escape(f"〔{labels[token[1]]}：{self.context[token[1]] if self.context[token[1]] is not None else '未提供'}〕"))
            elif len(token) == 2 and token[0] == "source" and token[1] in self.quotes:
                q = self.quotes[token[1]]
                matching = "，空白归一定位" if q["matching"] == "whitespace_normalized" else ""
                parts.append(f"〔来源原文 {_escape(q['source_id'])}，字符 {q['start']}–{q['end']}："
                             f"“{_escape(q['quote'])}”；原文摘录{matching}，非本次有效预测〕")
            else:
                raise ValueError("RESEARCH_NARRATIVE_REFERENCE_INVALID")
            end = match.end()
        parts.append(self._literal(text[end:]))
        return "".join(parts)

    def block(self, block):
        result = deepcopy(block)
        result["text"] = self.text(block["text"])
        # Sources referred to through a quote must still be visible in the block.
        result["evidence_refs"] = list(dict.fromkeys([*block["evidence_refs"], *[
            self.quotes[m[1][7:]]["source_id"] for m in _TOKEN.finditer(block["text"]) if m[1].startswith("source:")]]))
        return result


def change_view(applied):
    return [{"scenario_id": row["scenario_id"], "field": row["field"],
             "declared_basis": row["correction_basis"], "reason": row["reason"],
             "evidence_refs": row["evidence_refs"],
             "replacement_basis_type": (row["after"]["amount"] if row["field"] == "noncontrolling_attribution" else row["after"])["basis_type"]}
            for row in applied]


def report_context(report, draft, calculations, sources, request, *, changes, beliefs):
    context = NarrativeContext(draft, calculations, sources, report["source_quotes"], request)
    for block in (report["summary"], *report["financial_analysis"].values(), report["strongest_counterevidence"]):
        render_research_block(context.block(block), draft, calculations)
    for row in report["scenario_assessments"]:
        context.text(row["reason"])
        context.text(row["what_changes_the_view"])
    for text in report["limitations"]:
        context.text(text)
    for key, expected, identity in (
        ("change_explanations", changes, lambda r: (r["scenario_id"], r["field"])),
        ("belief_explanations", beliefs, lambda r: r["belief_id"])):
        rows = report[key]
        if len(rows) != len(expected) or {identity(r) for r in rows} != {identity(r) for r in expected}:
            raise ValueError("RESEARCH_EXPLANATION_COVERAGE_INVALID")
        for row in rows:
            render_research_block(context.block(row["explanation"]), draft, calculations)
    return context
