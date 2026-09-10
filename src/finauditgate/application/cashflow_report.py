"""Escaped local research workpapers; no financial decisions live here."""

from decimal import Decimal, localcontext
from html import escape

from finauditgate.core.ixbrl import FINANCIAL_CONTEXT


_ISSUES = {
    "ADJUSTMENT_HAS_UNTAGGED_COMPARATIVE_VALUE": "部分调整项有一期没有可读取的数值标签；保留已知数值，不把缺值当成零。",
    "CASHFLOW_BRIDGE_UNRECONCILED_CURRENT": "本期已提取调节项尚未与经营现金流勾稽。",
    "CASHFLOW_BRIDGE_UNRECONCILED_COMPARISON": "比较期已提取调节项尚未与经营现金流勾稽。",
    "CONSOLIDATED_CASHFLOW_STATEMENT_NOT_FOUND": "未找到当前支持的年度合并现金流量表及两期合并净利润。",
    "POST_CUTOFF_DOCUMENT": "报告的声明披露日期晚于研究截止日期。",
    "ADJUSTMENT_PERIOD_MISMATCH": "部分调整项与净利润的期间不一致，未混入计算。",
    "CONFLICTING_CASHFLOW_STATEMENTS": "发现数值冲突的现金流量表候选，需要人工选择来源。",
    "COUNTERPART_FACT_CONFLICT": "同文件的比较期候选存在数值冲突，未据此补齐。",
    "COUNTERPART_CASH_SIGN_UNRESOLVED": "找到比较期披露，但无法确定其现金流方向，未据此补齐。",
}
_ROLES = {"PERIOD_CHANGE_EXPLANATION": "本期变动说明候选", "PERIOD_FACT": "本期事实候选",
          "UNDATED_CHANGE_DESCRIPTION": "未明确期间的变动说明", "OTHER_PERIOD": "其他期间",
          "POLICY_BACKGROUND": "会计政策背景", "RISK_BACKGROUND": "风险提示背景",
          "RELATED_CONTEXT": "相关背景"}
_SIGNALS = {
    "POSITIVE_PROFIT_NEGATIVE_OPERATING_CASH": "本期合并净利润为正，经营现金流为负，需要进一步核实现金转化差异。",
    "PROFIT_AND_CASH_CHANGED_IN_OPPOSITE_DIRECTIONS": "合并净利润与经营现金流的同比变动方向相反。",
}
_FEEDBACK = {"CANDIDATE_DISCLOSURES_FOUND": "找到相关披露候选，原因仍待复核",
             "BACKGROUND_ONLY": "仅找到背景材料，未取得本期变动说明",
             "NO_MATCHING_DISCLOSURE": "本轮未找到匹配披露",
             "REPEATED_SEARCH_STOPPED": "重复补查已停止", "FINISHED": "已结束补查",
             "INVALID_ACTION": "模型动作格式不符，停止补查",
             "ACTION_NOT_ALLOWED": "模型未给出允许的动作，停止补查"}
_STYLE = """
:root{color-scheme:light}body{font:15px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;color:#24323c;background:#f2f4f5;margin:0}
main{max-width:1050px;margin:36px auto;background:white;padding:36px 44px;border:1px solid #dce2e5;border-radius:10px}
h1{font-size:28px;margin:8px 0}h2{font-size:19px;margin-top:28px}p{margin:10px 0}.muted,small{color:#647580}.badge{display:inline-block;background:#fff0cf;color:#755413;padding:3px 12px;border-radius:20px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{border-bottom:1px solid #e0e6e9;padding:10px 12px;text-align:right}th:first-child,td:first-child{text-align:left}
th{background:#f2f6f7}a{color:#176877;text-decoration:none}a:hover{text-decoration:underline}blockquote{border-left:3px solid #b8cbd0;padding-left:18px;white-space:pre-wrap;overflow-wrap:anywhere}
.flow{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0}.flow span{background:#edf4f4;padding:7px 12px;border-radius:4px}.notice{background:#fff8e8;padding:14px 18px;border-left:3px solid #ccaa58}
details{border:1px solid #dce2e5;padding:12px 16px;margin:10px 0}summary{cursor:pointer}code{font-size:12px;overflow-wrap:anywhere}
@media(max-width:700px){main{margin:0;padding:22px 16px;border:0}table{font-size:12px}th,td{padding:8px 5px}h1{font-size:24px}}
@media print{body{background:white}main{border:0;margin:0;padding:0}details{break-inside:avoid}}
"""


def _page(title, content):
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>{escape(title)}</title><style>{_STYLE}</style><main>{content}</main></html>").encode()


def _amount(value):
    if value is None:
        return "未提取"
    with localcontext(FINANCIAL_CONTEXT):
        return format(Decimal(value) / Decimal(1000000), ",.2f")


def _links(ids):
    return " ".join(f"<a href='evidence.html#{escape(i, quote=True)}'>[{n + 1}]</a>" for n, i in enumerate(ids))


def render(record):
    a, task = record["analysis"], record["task"]
    interim = task.get("source_format") == "INTERIM_HTML_JANUARY_JUNE"
    period_label = "上半年合并口径 · 同期比较，未年化" if interim else "年度合并口径"
    source_url = escape(task["source_url"], quote=True)
    intro = (f"<span class='badge'>研究草稿 · 待人工复核</span><h1>盈利与现金流调查</h1>"
             f"<p>{escape(task['document']['source_id'])} · {escape(a['current_end'])} 对比 {escape(a['comparison_end'])}</p>"
             f"<p class='muted'>{period_label} · 单位：{escape(a['currency'])} 百万 · <a href='{source_url}'>打开原始披露</a></p>"
             "<div class='flow'><span>① 读取来源事实</span><span>② 核算现金差异</span><span>③ 选择附注补查</span><span>④ 人工复核底稿</span></div>")
    signals = "".join(f"<p>{escape(_SIGNALS[s])}</p>" for s in a.get("signals", []))
    rows = []
    labels = {"profit": "合并净利润（包含少数股东损益）", "operating_cashflow": "经营活动现金流",
              "cash_minus_profit": "经营现金流 − 合并净利润", "unexplained_residual": "扣除已提取调节项后的余额"}
    for key in labels:
        if key not in a["metrics"]:
            continue
        metric = a["metrics"][key]
        rows.append(f"<tr><td>{labels[key]} {_links(metric.get('fact_ids', []))}</td><td>{_amount(metric['current'])}</td><td>{_amount(metric['comparison'])}</td><td>{_amount(metric.get('change'))}</td></tr>")
    table = ("<h2>两期事实与差异</h2><table><thead><tr><th>项目</th><th>本期</th><th>比较期</th><th>金额变化</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>") if rows else "<div class='notice'>未取得足够的同口径事实，本次不生成财务计算结论。</div>"
    issues = "".join(f"<li>{escape(_ISSUES.get(code, '需要复核：' + code))}</li>" for code in a["issues"])
    notice = ("<div class='notice'><strong>取证限制</strong><ul>" + issues + "</ul><small>余额为零只说明已提取金额能够相加；存在缺项时不表示整张表已经核全。</small></div>") if issues else ""
    if a['resolved_values']:
        notice = f"<p class='muted'>已通过同一份报告其他位置的明确披露补齐 {len(a['resolved_values'])} 处数值；每处均附来源，不使用余额倒推。</p>" + notice
    overview = "<h2>经营现金流变动：报告如何解释</h2>"
    current_explanations = [h for h in a['issuer_overview'] if h['evidence_role'] == 'PERIOD_CHANGE_EXPLANATION']
    if current_explanations:
        for hit in current_explanations:
            overview += f"<blockquote>{escape(hit['text'])}</blockquote><p><a href='evidence.html#{escape(hit['note_id'],quote=True)}'>原文与定位</a> <small>自动匹配的本期说明，因果关系仍须复核。</small></p>"
    else:
        overview += "<p class='muted'>尚未找到本期经营现金流变动的明确说明；已有命中不能直接当作本期原因。</p>"
    group = a['operating_assets_liabilities']
    if group:
        overview += ("<table><thead><tr><th>现金流表分组</th><th>本期</th><th>比较期</th><th>金额变化</th></tr></thead><tbody>"
                     f"<tr><td>已提取经营性资产及负债变动 <a href='evidence.html#operating-assets-liabilities'>[组成项]</a></td><td>{_amount(group['current'])}</td><td>{_amount(group['comparison'])}</td><td>{_amount(group['change'])}</td></tr></tbody></table>")
        if not group['complete']:
            overview += "<p class='muted'>该分组仍有取证缺项，展示的是已提取部分，未计算完整变动。</p>"
    check = a['management_check']
    if check:
        verdict = '在披露的四舍五入范围内吻合' if check['within_rounding'] else '未在披露的四舍五入范围内吻合，需复核口径'
        overview += (f"<p>公司披露的变动为 <strong>{_amount(check['reported_change'])}</strong>，表内分组计算为 "
                     f"<strong>{_amount(check['calculated_change'])}</strong>，差额 {_amount(check['difference'])}：{verdict}。</p>"
                     "<p class='muted'>这里只检查数字是否相容；公司所称营运资本与该现金流表分组的定义、以及因果解释仍须复核。</p>")
    drivers = ""
    if a["drivers"]:
        recovered = {r['driver_id'] for r in a['resolved_values']}
        driver_rows = "".join(f"<tr><td>{escape(d['label'])} {_links(d['fact_ids'])}{'<br><small>含同文件其他位置补齐的数值</small>' if d['driver_id'] in recovered else ''}</td><td>{_amount(d['current'])}</td><td>{_amount(d['comparison'])}</td><td>{_amount(d['change'])}</td></tr>" for d in a["drivers"][:8])
        drivers = ("<h2>优先复核的调节项目</h2><p class='muted'>按金额变动排序；缺少一侧数值时按已知金额确定补查优先级。数值采用报表展示的现金影响方向。</p>"
                   "<table><thead><tr><th>项目与证据</th><th>本期</th><th>比较期</th><th>金额变化</th></tr></thead><tbody>" + driver_rows + "</tbody></table>")
    by_driver = {d["driver_id"]: d for d in a["drivers"]}
    searches, evidence, note_seen = [], [], set()
    def add_note(hit, label):
        if hit['note_id'] in note_seen:
            return
        note_seen.add(hit['note_id'])
        role = _ROLES[hit['evidence_role']]
        evidence.append(f"<section id='{escape(hit['note_id'],quote=True)}'><h2>{escape(label)}</h2><p class='muted'>{role} · 原文标题：{escape(hit['heading'] or '未定位标题')}</p><blockquote>{escape(hit['text'])}</blockquote><p class='muted'>原文字符区间 {hit['source']['char_start']}–{hit['source']['char_end']}（UTF-8 解码后的字符位置）</p></section>")
    for hit in a['issuer_overview']:
        add_note(hit, '经营现金流概览')
    mode = "固定规则补查" if task["strategy"] == "rules" else "本地 8B 选择补查方向"
    for n, step in enumerate(record["steps"], 1):
        driver = by_driver.get(step["action"].get("driver_id"), {})
        label = driver.get("label", "结束或停止")
        hits = []
        for hit in step["hits"]:
            anchor = escape(hit["note_id"], quote=True)
            hits.append(f"<p><small>{_ROLES[hit['evidence_role']]}</small><br>{escape(hit['text'][:450])}… <a href='evidence.html#{anchor}'>查看完整披露与定位</a></p>")
            add_note(hit,label)
        origin = {'RULES':'规则选择','MODEL':'模型选择','RULE_FALLBACK':'背景命中后规则回退',
                  'RESEARCH_REQUEST':'研究步骤请求 · 程序检索'}[step['choice_origin']]
        searches.append(f"<details><summary>补查 {n}：{escape(label)} — {origin} — {escape(_FEEDBACK.get(step['feedback'],step['feedback']))}</summary>{''.join(hits) or '<p>本步骤没有新增来源证据。</p>'}</details>")
    investigation = (f"<h2>附注调查 · {mode}</h2><p class='muted'>以下为检索到的原文候选。匹配相关术语不等于解释成立，尤其需检查披露期间、业务范围与因果关系。</p>" + "".join(searches))
    for fact in a["facts"]:
        resolution = fact.get('resolution')
        method = '' if resolution is None else '<p>此数值来自同文件的相同指标、期间、主体与币种披露。' + ('来源带有明确的零值标签。' if resolution['method']=='EXPLICIT_TAGGED_ZERO' else '现金流方向沿用该现金流行中已有数值的符号关系。') + '</p>'
        if interim:
            period = (escape(fact['period_start']) + " 至 " if fact['period_start'] else "期末 ") + escape(fact['period_end'])
            headers = "；".join(str(h['char_start']) + "–" + str(h['char_end']) for h in fact['source_headers'])
            evidence.append(f"<section id='{escape(fact['fact_id'],quote=True)}'><h2>{escape(fact['row_label'])}</h2><p>{period} · {escape(fact['currency'])}</p><p>原文数字：<strong>{escape(fact['printed'])}</strong>；HTML 表格归一化金额：{escape(fact['value'])}</p><p>类型：{escape(record['reference_meanings'][fact['fact_id']]['label'])}。这是带表头定位的普通表格取数，不是 XBRL 标签验证。</p><p class='muted'>原行字符区间 {fact['source']['char_start']}–{fact['source']['char_end']}；表头区间 {headers}；原行 SHA-256：{fact['source']['span_sha256']}</p></section>")
        else:
            evidence.append(f"<section id='{escape(fact['fact_id'], quote=True)}'><h2>{escape(fact['row_label'])}</h2><p>{escape(fact['period_start'])} 至 {escape(fact['period_end'])} · {escape(fact['currency'])}</p><p>原文数字：<strong>{escape(fact['printed'])}</strong>；XBRL 归一化金额：{escape(fact['value'])}；现金流口径金额：{escape(fact['displayed_cash_effect'])}</p>{method}<p><code>{escape(fact['concept'])}</code></p><p class='muted'>原文字符区间 {fact['source']['char_start']}–{fact['source']['char_end']} · 片段 SHA-256：{fact['source']['span_sha256']}</p></section>")
    if group:
        group_rows = ''.join(f"<tr><td>{escape(d['label'])} {_links(d['fact_ids'])}</td><td>{_amount(d['current'])}</td><td>{_amount(d['comparison'])}</td><td>{_amount(d['change'])}</td></tr>" for d in a['drivers'] if d['group']=='OPERATING_ASSETS_LIABILITIES')
        evidence.append("<section id='operating-assets-liabilities'><h2>经营性资产及负债变动组成项</h2><table><tr><th>项目</th><th>本期</th><th>比较期</th><th>金额变化</th></tr>" + group_rows + f"</table><p>分组标题原文字符区间：{group['section_source']['char_start']}–{group['section_source']['char_end']}。此分组来自原现金流表，不是资产负债表余额差。</p></section>")
    footer = "<h2>复核要点</h2><p>核对未提取的比较期数值；确认相关披露是否解释了本次金额变化；区分公司解释与已验证事实。当前底稿不自动作出投资判断或批准研究变更。</p>"
    if record.get("schema_version", "").startswith("finauditgate.research-evidence/"):
        for driver in a["drivers"]:
            evidence.append(f"<section id='{escape(driver['driver_id'],quote=True)}'><h2>{escape(driver['label'])}：调节项目</h2>"
                f"<p>本期调节金额 {_amount(driver['current'])}；比较期 {_amount(driver['comparison'])}；同比变化 {_amount(driver['change'])}。单位：{escape(a['currency'])} 百万。</p>"
                f"<p>这是间接法调节项目，并非总收款或期末余额。组成事实：{_links(driver['fact_ids'])}</p></section>")
    return (_page("盈利与现金流调查", intro + signals + table + notice + overview + drivers + investigation + footer),
            _page("来源证据", f"<h1>来源证据</h1><p><a href='workpaper.html'>返回底稿</a> · <a href='{source_url}'>原始披露</a></p>" + "".join(evidence)))
