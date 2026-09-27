"""Evidence-block selection and local evidence findings for thesis reports.

The catalog preserves every source character. Binding proves an address in the
supplied source, not financial truth, relevance, or the sufficiency of context.
Calculation, unknown-number and explanation-coverage failures remain hard errors.
"""

from copy import deepcopy
from datetime import date
import re
import unicodedata

from finauditgate.adapters.thesis_protocol import source_view
from finauditgate.core.artifacts import sha256_hex
from . import research_narrative as narrative
from .research_numbers import METRIC_KEYS, render_research_block


INSTRUCTION = (
    "终稿使用资料中程序给定的证据块ID。历史数量用{{source:E0001}}这样的引用，"
    "不要重新摘抄原文、填source_quotes或猜测字符位置；程序复制真实片段并标明来源。"
    "引用前结合相邻块核对期间、单位、集团/分部及合并/归母口径；不能把单块存在当成含义获证实。"
    "需要多个块时分别引用，不拼接为一条虚构原文。"
    "关键前瞻数量用{{metric:F1:eps_per_traded_unit}}，程序插入有效值、名称、单位和期间。"
    "正文中的来源列表和指标列表由程序派生，通常只需填写text；不用重复维护evidence_refs和metrics。"
    "不要直接重写财务金额、比率、目标价或中文数额。明确的四位年份加年、与研究期限相同且用于时间描述的月数、以冒号引出内容的中文列举可直接写；"
    "它们只是时间或行文标签，不证明对应事实。Q1至Q4可用于季度数据/业绩等时间描述，不能当数值。"
    "日期与代码使用{{context:as_of}}、{{context:symbol}}、{{context:forecast_start}}、"
    "{{context:forecast_end}}、{{context:valuation_date}}、{{context:market_price_date}}、{{context:horizon_months}}。"
    "不用HTML、Markdown链接或自造引用语法。所有reason、what_changes_the_view、limitations和explanation.text遵守同一契约。"
    "change_explanations仅且完整覆盖change_context中的scenario_id/field；belief_explanations仅且完整覆盖已给belief_id。"
    "不重作修正、改评级依据或把数学复算当作财务事实认证。"
)


def evidence_catalog(sources):
    """Address contiguous source chunks, without cleaning, joining or dropping text."""
    entries = []
    for source in sources["sources"]:
        if source.get("use", "research") != "research":
            continue
        body = source["content"]
        digest = sha256_hex(body.encode())
        if "sha256" in source and source["sha256"] != digest:
            raise ValueError("THESIS_SOURCE_RECORD_INVALID")
        start = 0
        while start < len(body):
            end = min(start + 1600, len(body))
            if end < len(body):
                line = body.rfind("\n", start + 400, end)
                if line >= 0:
                    end = line + 1
            entries.append({"id": f"E{len(entries) + 1:04d}", "source_id": source["id"],
                "source_sha256": digest, "start": start, "end": end, "text": body[start:end]})
            start = end
    return entries


def final_source_view(sources):
    """Replace source content with addressed blocks; do not duplicate its text."""
    view = source_view(sources)
    entries = evidence_catalog(view)
    for source in view["sources"]:
        del source["content"]
        source["content_blocks"] = [{"id": e["id"], "text": e["text"]}
            for e in entries if e["source_id"] == source["id"]]
    return view


def _metric_refs(refs):
    if not isinstance(refs, list):
        raise ValueError("THESIS_METRIC_SELECTION_INVALID")
    out = []
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"scenario_id", "metric"}
                or ref["scenario_id"] not in ("F1", "F2", "F3") or ref["metric"] not in METRIC_KEYS):
            raise ValueError("THESIS_METRIC_SELECTION_INVALID")
        if ref not in out:
            out.append(deepcopy(ref))
    return out


def _inline_metrics(text):
    return [{"scenario_id": m[1], "metric": m[2]}
        for m in re.finditer(r"\{\{metric:([^:{}]+):([^:{}]+)\}\}", text)]


def normalize_report(candidate, sources, *, legacy=False):
    """Derive redundant selectors; preserve all prose and every explicit metric.

    legacy=True is for explicit offline saved-output inspection, never the model
    protocol or historical Case reader. It keeps original quotations for review.
    """
    report = deepcopy(candidate)
    if not isinstance(report, dict) or ("source_quotes" in report and not legacy):
        raise ValueError("THESIS_FINAL_SELECTION_FORMAT_INVALID")
    expected = {"rating", "summary", "financial_analysis", "strongest_counterevidence", "scenario_assessments",
                "limitations", "change_explanations", "belief_explanations"}
    if (set(report) != expected | ({"source_quotes"} if legacy else set())
            or report["rating"] not in ("Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW")
            or set(report["financial_analysis"]) != {"operating_performance", "earnings_quality",
                "cash_and_capital_allocation", "valuation_and_price_requirements"}):
        raise ValueError("THESIS_FINAL_SELECTION_FORMAT_INVALID")
    catalog = {e["id"]: e["source_id"] for e in evidence_catalog(sources)}
    source_ids = {s["id"] for s in sources["sources"]}
    aliases = {q["id"]: q["source_id"] for q in report.get("source_quotes", [])} if legacy else {}
    lookup = {**catalog, **aliases}

    def block(value):
        if (not isinstance(value, dict) or not {"text"} <= value.keys()
                or not value.keys() <= {"text", "metrics", "evidence_refs"}
                or not isinstance(value["text"], str)):
            raise ValueError("THESIS_RESEARCH_BLOCK_INVALID")
        refs = value.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise ValueError("THESIS_SOURCE_SELECTION_INVALID")
        ids = re.findall(r"\{\{source:([^:{}]+)\}\}", value["text"])
        # Unknown IDs remain visible for local review; they never gain a source.
        # evidence_refs denotes sources; a real source called E0001 or Q1 must
        # not be silently redirected to an unrelated evidence-block alias.
        # The v14 field always denotes sources, including unknown/excluded
        # source IDs. Only explicit legacy Q aliases may be projected here.
        source_refs = [r if not legacy or r in source_ids else aliases.get(r, r) for r in refs]
        value["evidence_refs"] = list(dict.fromkeys([*source_refs, *(lookup.get(r, r) for r in ids)]))
        if len(value["evidence_refs"]) > 48:
            raise ValueError("THESIS_SOURCE_SELECTION_LIMIT")
        value["metrics"] = _metric_refs([*value.get("metrics", []), *_inline_metrics(value["text"])])

    for value in (report["summary"], *report["financial_analysis"].values(), report["strongest_counterevidence"]):
        block(value)
    for key in ("change_explanations", "belief_explanations"):
        for row in report[key]:
            block(row["explanation"])
    for row in report["scenario_assessments"]:
        row["metrics"] = _metric_refs([*row.get("metrics", []),
            *_inline_metrics(row["reason"]), *_inline_metrics(row["what_changes_the_view"])])
    return report


# Words whose adjacent number is a financial or change quantity. The narrow
# list also governs longer-distance phrasing, as in the original contract.
_NARROW_VALUE_WORDS = r"(?:EPS|每股收益|每股盈利|收入|利润率?|回报|股价|目标价|价格|金额|股息|市盈率|现金)"
_VALUE_WORDS = (r"(?:EPS|PE|PB|PS|ROE|ROA|ROIC|EBITDA|EBIT|FCFE|FCF|DPS|BVPS|TTM|每股|收入|营收|利润|毛利|净利|扣非|归母|"
                r"回报|收益|股价|目标价|价格|金额|股息|分红|市盈率|市净率|市值|估值|现金|资产|负债|股本|存货|应收|应付|费用|成本|"
                r"增速|增幅|降幅|幅度|增长|下降|上升|提升|提高|降低|减少|增加|回落|下滑|上涨|下跌|改善|恶化|扩大|收窄|"
                r"同比|环比|占比|比例|比率|倍数|利率|税率|规模|总额|余额|水平|空间)")
_EXPLICIT = re.compile(_VALUE_WORDS + r"[ \t]*[为是约达有=＝:：][ \t]*[\u4e00-\u9fffA-Za-z]{0,4}$", re.I)
_ADJACENT = re.compile(_VALUE_WORDS + r"[ \t]*(?:约为|约|达到|达|至|到|了|超过|超|近|逾|仅|高达|低至|从|由)?[ \t]*$", re.I)
_DISTANT = re.compile(_NARROW_VALUE_WORDS + r"[^。；，,:：\n]{0,16}(?:为|是|至|达|到|约|=|＝)[ \t]*$", re.I)
_COUNT = re.compile(r"(?:个?(?:交易日|工作日|月|季度|星期|方面|部分)|年|天|日|周|次|家|项|名|条|位|只|款|代|层|类|种|点)")
_UNIT_AFTER = (r"(?:(?:个?(?:交易日|月|季度)|年|天|日|H[12]|Q[1-4])[ \t）)」』”’\"']*)?[ \t]*"
               r"(?:个?百分点|%|‰|元|美元|港元|人民币|倍|成|万|亿|千|百万|股|bps?\b|USD|CNY|RMB|HKD|SGD|EUR|GBP|JPY")
_VALUE_SUFFIX = re.compile(r"(?:x|bps?|bn|mn)", re.I)
_FINANCIAL_ABBREVIATION = re.compile(r"(?:EPS|PE|PB|ROE|ROA|ROIC|EBITDA|EBIT|FCFE|FCF|DPS|BVPS|TTM)", re.I)
_NUMERAL = re.compile(r"(?P<letters>[A-Za-z]*)(?P<digits>[0-9]+(?:[.,][0-9]+)*)(?P<suffix>(?:[A-Za-z]+[0-9]*)*)(?P<percent>[%‰]?)")
_ISO_DATE = re.compile(r"(?<![0-9])[12][0-9]{3}-[01]?[0-9]-[0-3]?[0-9](?![0-9])")


def _wrapping(char):
    return char.isspace() or char in "\"'" or unicodedata.category(char) in {"Ps", "Pe", "Pi", "Pf"}


class DeliveryContext(narrative.NarrativeContext):
    """Keep unresolved evidence local and visible; never invent a locator."""

    def __init__(self, draft, calculations, sources, report, request, *, legacy=False):
        super().__init__(draft, calculations, sources, [], request)
        self.findings = []
        self.locations = {}
        self._field = ""
        self._record_findings = True
        self._unresolved = {}
        self._hypothesis_count = len(request.get("hypotheses", []))
        if legacy:
            seen = set()
            for i, q in enumerate(report.get("source_quotes", [])):
                if (not isinstance(q, dict) or set(q) != {"id", "source_id", "quote"}
                        or not isinstance(q["id"], str) or not re.fullmatch(r"Q[1-9][0-9]?", q["id"])
                        or q["id"] in seen or not isinstance(q["quote"], str) or not 12 <= len(q["quote"]) <= 1200):
                    raise ValueError("RESEARCH_SOURCE_QUOTES_INVALID")
                seen.add(q["id"])
                source = self.sources.get(q["source_id"])
                try:
                    if source is None:
                        raise ValueError("THESIS_UNKNOWN_SOURCE_REFERENCE")
                    start, end, matching = narrative._locate_quote(source["content"], q["quote"])
                    self.quotes[q["id"]] = {**q, "quote": source["content"][start:end],
                        "start": start, "end": end, "matching": matching}
                except ValueError as exc:
                    self._unresolved[q["id"]] = {"source_id": q["source_id"], "reason": str(exc)}
                    # Explicit old selections remain reviewable even when the
                    # model forgot to use them in prose. Do not hide their
                    # failed binding in a raw-output appendix.
                    self._field = f"source_quotes[{i}]"
                    self._finding(q["id"], str(exc), q["source_id"])
                    self._field = ""
        else:
            self.quotes = {e["id"]: {"id": e["id"], "source_id": e["source_id"], "quote": e["text"],
                "start": e["start"], "end": e["end"], "matching": "catalog"} for e in evidence_catalog(sources)}

    def _literal(self, text):
        for token in self._classify(self._checked(text)):
            self._finding(token, "UNBOUND_RESEARCH_NUMBER_PENDING")
        return narrative._escape(text)

    def unbound_numbers(self, text):
        """Pending numerals in one prose segment; value positions still raise."""
        return self._classify(self._checked(text))

    def _checked(self, text):
        checked = unicodedata.normalize("NFKC", text)
        # Classify complete time labels, not their digits in isolation. The
        # source text is returned unchanged and still is not a verified fact.
        value_prefix = re.compile(r"(?:EPS|每股收益|每股盈利|收入|利润率?|回报|股价|目标价|价格|金额|股息|市盈率|现金)"
            r"(?:[ \t]*[为是约达有=＝:：]+[ \t]*|[^。；，,:：\n]{0,16}(?:为|是|至|达|到|约|=|＝))$", re.I)
        temporal = re.compile(r"(?<![A-Za-z0-9_.=＝])(?:[12][0-9]{3}年(?:"
            r"(?:0?[1-9]|1[0-2])月(?:末|(?:0?[1-9]|[12][0-9]|3[01])日)?)?|"
            r"(?:[12][0-9]{3})?Q[1-4]|"
            + str(self.context["horizon_months"]) + r"个月)(?![A-Za-z0-9.%％元美港币万亿倍])")
        def wrapping(char):
            return char.isspace() or char in "\"'" or unicodedata.category(char) in {"Ps", "Pe", "Pi", "Pf"}

        def value_position(match):
            left = checked[:match.start()]
            right = checked[match.end():]
            while left and wrapping(left[-1]):
                left = left[:-1]
            while right and wrapping(right[0]):
                right = right[1:]
            units = r"(?:元|美元|港元|人民币|%|％|倍|成|万|亿|百万|千|百分点|USD|CNY|RMB|HKD|SGD|EUR|GBP|JPY|" + re.escape(self.draft["reporting_currency"]) + "|" + re.escape(self.draft["price_currency"]) + ")"
            return bool(value_prefix.search(left) or re.match(units, right, re.I))

        # Fiscal/half-year labels and report years are dates, not amounts.
        # Recognize the bare first year only in an explicit paired year label.
        paired_year = re.compile(r"(?<![A-Za-z0-9_.=＝])[12][0-9]{3}(?=和[12][0-9]{3}H[12](?![A-Za-z0-9.]))")
        checked = paired_year.sub(lambda m: m[0] if value_position(m) else "所述年度", checked)
        fiscal = re.compile(r"(?<![A-Za-z0-9_.=＝])(?:FY[12][0-9]{3}|[12][0-9]{3}H[12]|"
            r"[12][0-9]{3}(?=半年报|年报|季报))(?![A-Za-z0-9.%％元美港币万亿倍])")
        checked = fiscal.sub(lambda m: m[0] if value_position(m) else "所述期间", checked)
        hypothesis = re.compile(r"假设([1-9][0-9]?)(?![A-Za-z0-9.%％元美港币万亿倍])")
        checked = hypothesis.sub(lambda m: "已登记假设" if int(m[1]) <= self._hypothesis_count
            and not value_position(m) else m[0], checked)
        hypothesis_id = re.compile(r"(?<![A-Za-z0-9_])H([1-9][0-9]?)(?![A-Za-z0-9_.%％元美港币万亿倍])")
        checked = hypothesis_id.sub(lambda m: "已登记假设" if int(m[1]) <= self._hypothesis_count
            and not value_position(m) else m[0], checked)
        year_before_word = re.compile(r"(?<![A-Za-z0-9_.=＝])[12][0-9]{3}年(?=[A-Za-z])")
        checked = year_before_word.sub(lambda m: m[0] if value_position(m) else "所述年度", checked)
        checked = temporal.sub(lambda m: m[0] if value_position(m) else "所述期间", checked)
        quant = self.sources.get("QUANT", {})
        quant_days = set(re.findall(r"预测期限为([1-9][0-9]{0,2})个交易日", quant.get("content", "")))
        trading_days = re.compile(r"(?<![A-Za-z0-9_.])([1-9][0-9]{0,2})个交易日(?![A-Za-z0-9_.%％元美港币万亿倍])")
        checked = trading_days.sub(lambda m: "Quant已注明的交易日期限" if m[1] in quant_days
            and not value_position(m) else m[0], checked)
        retrieved_dates = set()
        for source in self.sources.values():
            metadata = source["content"] + "\n" + source.get("availability_note", "")
            for value in re.findall(r"(?:抓取时间[：:]|抓取于)([12][0-9]{3}-[0-9]{2}-[0-9]{2})(?![0-9])", metadata):
                try:
                    date.fromisoformat(value)
                except ValueError:
                    continue
                retrieved_dates.add(value)
        retrieval = re.compile(r"抓取于([12][0-9]{3}-[0-9]{2}-[0-9]{2})(?![A-Za-z0-9_.%％元美港币万亿倍])")
        checked = retrieval.sub(lambda m: "抓取于来源已记录日期" if m[1] in retrieved_dates
            and not value_position(m) else m[0], checked)
        # A source-proven storage-standard version is an identifier, not an
        # amount. Unknown versions and monetary uses remain numeric failures.
        versions = re.compile(r"(?<![A-Za-z0-9_])UFS[ \t\r\n]*[0-9]+(?:\.[0-9]+){1,2}(?![A-Za-z0-9_]|\.[0-9])")
        source_versions = {re.sub(r"\s+", "", m[0]) for source in self.sources.values()
            for m in versions.finditer(unicodedata.normalize("NFKC", source["content"]))}
        checked = versions.sub(lambda m: "资料中的技术版本" if not value_position(m)
            and re.sub(r"\s+", "", m[0]) in source_versions else m[0], checked)
        # Bind a common input explicitly, rather than treating the digit one
        # as an unverified free-form financial value.
        from decimal import Decimal
        ratio = self.draft["shares_per_traded_unit"]["value"]
        units = re.compile(r"每交易单位([0-9]+(?:\.[0-9]+)?)股普通股")
        checked = units.sub(lambda m: "每交易单位对应已绑定普通股数" if not value_position(m)
            and ratio is not None and Decimal(m[1]) == Decimal(str(ratio)) else m[0], checked)
        enumeration = re.compile(r"(?:有|以下|包括|分为)([一二两三四五六七八九十]{1,3})(?:点|项|方面|部分)"
            r"(?=[:：]|(?:需要|值得)[^。；:：0-9零一二两三四五六七八九十百千万亿]{1,12}[:：]|"
            r"较(?:扎实|重要|明显|突出|明确)[:：])")
        checked = enumeration.sub("所列方面", checked)
        heading = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9_.=＝])[一二两三四五六七八九十]{1,3}点提醒[:：]")
        checked = heading.sub(lambda m: m[0] if value_position(m) else "所列提醒：", checked)
        return checked

    def _classify(self, checked):
        """Refuse numerals in value positions; return the other unbound ones.

        Value positions are a financial or change word before the number, a
        currency, ratio, multiple or share unit after it, and decimals,
        percentages, grouped or oversized amounts. Any other numeral that no
        typed rule bound (an unregistered label, version or count) is returned
        so the saved report stays visibly PARTIAL rather than being halted or
        silently accepted. Every numeral the strict v13 contract refused is
        therefore either refused here or returned as pending.
        """
        clean = self.identifiers.sub("", unicodedata.normalize("NFKC", checked))
        currencies = "|".join(re.escape(c) for c in {self.draft["reporting_currency"], self.draft["price_currency"]} if c)
        unit = re.compile(_UNIT_AFTER + (r"|(?:" + currencies + r")" if currencies else "") + r")", re.I)
        pending = []

        def sides(start, end):
            left, right = clean[:start], clean[end:]
            while left and _wrapping(left[-1]):
                left = left[:-1]
            while right and _wrapping(right[0]):
                right = right[1:]
            return left, right

        def valued(left, right, small):
            return bool(unit.match(right) or _EXPLICIT.search(left) or _DISTANT.search(left)
                        or re.match(_NARROW_VALUE_WORDS, right)
                        or (_ADJACENT.search(left) and not (small and _COUNT.match(right))))

        scan = clean
        for match in _ISO_DATE.finditer(clean):
            left, right = sides(match.start(), match.end())
            if unit.match(right) or _EXPLICIT.search(left) or _ADJACENT.search(left):
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            pending.append(match[0])
            scan = scan[:match.start()] + "\u25a1" * (match.end() - match.start()) + scan[match.end():]
        for match in _NUMERAL.finditer(scan):
            letters, digits, suffix, percent = match["letters"], match["digits"], match["suffix"], match["percent"]
            left, right = sides(match.start(), match.end())
            if letters:
                hard = bool(percent or _FINANCIAL_ABBREVIATION.fullmatch(letters) or unit.match(right)
                            or _EXPLICIT.search(left))
            else:
                grouped = any(c in digits for c in ".,")
                year_like = not grouped and len(digits) == 4 and 1990 <= int(digits) <= 2099
                # A year or fiscal period next to a financial word is still a
                # period label unless a connector or unit makes it a value.
                period = year_like and (re.fullmatch(r"(?:H[12]|Q[1-4])?", suffix) is not None) and (
                    bool(suffix) or right.startswith(("年", "财年", "年度")) or not re.match(r"[0-9]", right))
                hard = bool(percent or grouped or _VALUE_SUFFIX.fullmatch(suffix) or len(digits) >= 5
                            or (len(digits) == 4 and not year_like)
                            or (unit.match(right) or _EXPLICIT.search(left) if period
                                else valued(left, right, small=len(digits) <= 3)))
            if hard:
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            pending.append(match[0])
        for match in narrative._CN_AMOUNT.finditer(clean):
            token = match[0]
            left, right = sides(match.start(), match.end())
            if (token.startswith(("百分之", "千分之")) or re.search(r"[百千万亿兆佰仟]|点[零〇一二两三四五六七八九十]", token)
                    or re.search(r"(?:倍|元|美元|港元|人民币|成)$", token) or valued(left, right, small=True)):
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            pending.append(token)
        for match in narrative._EN_AMOUNT.finditer(clean):
            if re.fullmatch(r"(?i:hundred|thousand|million|billion|trillion)", match[0]):
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            pending.append(match[0])
        if narrative._CN_FINANCIAL_NUMBER.search(clean) or any(
                c.isnumeric() and not c.isascii() and not "\u4e00" <= c <= "\u9fff" for c in clean):
            raise ValueError("UNBOUND_RESEARCH_NUMBER")
        if any(c in clean for c in ("{", "}", "<", ">", "`", "[", "]", "\\")):
            raise ValueError("RESEARCH_NARRATIVE_MARKUP_INVALID")
        return list(dict.fromkeys(pending))

    def _finding(self, reference, reason, source_id=None):
        if not self._record_findings:
            return
        value = {"field": self._field, "reference": reference, "source_id": source_id, "reason": reason}
        if value not in self.findings:
            self.findings.append(value)

    def source(self, qid):
        if qid in self.quotes:
            if self._record_findings:
                self.locations.setdefault(qid, set()).add(self._field)
            return super().source(qid)
        info = self._unresolved.get(qid, {"source_id": None, "reason": "THESIS_UNKNOWN_EVIDENCE_BLOCK"})
        self._finding(qid, info["reason"], info["source_id"])
        return "〔证据待核：" + narrative._escape(qid) + "；未提供已核原文或位置，不可作为已证实依据〕"

    def block(self, block):
        value = deepcopy(block)
        value["text"] = self.text(block["text"])
        for ref in block["evidence_refs"]:
            if ref not in self.sources:
                self._finding(ref, "THESIS_UNKNOWN_SOURCE_REFERENCE")
        # Unknown references are displayed explicitly, never silently repaired.
        value["evidence_refs"] = [r if r in self.sources else "证据待核：" + r for r in block["evidence_refs"]]
        return value

    def evidence_check(self):
        bindings = []
        for qid, fields in sorted(self.locations.items()):
            q = self.quotes[qid]
            bindings.append({"reference": qid, "source_id": q["source_id"], "start": q["start"], "end": q["end"],
                "source_sha256": sha256_hex(self.sources[q["source_id"]]["content"].encode()),
                "fields": sorted(fields)})
        return {"status": "PARTIAL" if self.findings else "COMPLETED", "scope": "SOURCE_BINDING_ONLY_NOT_SEMANTIC_APPROVAL",
            "findings": deepcopy(self.findings), "bindings": bindings}


def report_context(report, draft, calculations, sources, request, *, changes, beliefs, legacy=False):
    context = DeliveryContext(draft, calculations, sources, report, request, legacy=legacy)

    def block(value, field):
        context._field = field
        render_research_block(context.block(value), draft, calculations)

    def text(value, field):
        context._field = field
        context.text(value)

    block(report["summary"], "summary")
    # Saved Cases are canonical JSON with sorted keys; findings must not depend on generation order.
    for name, value in sorted(report["financial_analysis"].items()):
        block(value, "financial_analysis." + name)
    block(report["strongest_counterevidence"], "strongest_counterevidence")
    for i, row in enumerate(report["scenario_assessments"]):
        text(row["reason"], f"scenario_assessments[{i}].reason")
        text(row["what_changes_the_view"], f"scenario_assessments[{i}].what_changes_the_view")
        render_research_block({"text": "", "evidence_refs": [], "metrics": row["metrics"]}, draft, calculations)
    for i, value in enumerate(report["limitations"]):
        text(value, f"limitations[{i}]")
    for key, expected, identity in (
            ("change_explanations", changes, lambda r: (r["scenario_id"], r["field"])),
            ("belief_explanations", beliefs, lambda r: r["belief_id"])):
        rows = report[key]
        if len(rows) != len(expected) or {identity(r) for r in rows} != {identity(r) for r in expected}:
            raise ValueError("RESEARCH_EXPLANATION_COVERAGE_INVALID")
        for i, row in enumerate(rows):
            block(row["explanation"], f"{key}[{i}].explanation")
    context._field = ""
    context._record_findings = False
    return context
