"""Program-owned comparison of proposed inputs; never a verdict on their rationale."""

from copy import deepcopy

from finauditgate.core.forward_revision import COMMON_FIELDS, SCENARIO_FIELDS, apply_forward_revision
from .forward_report import _table
from . import thesis_report_v11 as display


def _value(field, assumption):
    if field == "noncontrolling_attribution":
        return {"nature": assumption["nature"], "amount": assumption["amount"]["value"]}
    return assumption["value"]


def parameter_change_facts(draft, changes):
    """Use the same revision engine as the Case, including no-ops and unknowns."""
    result = apply_forward_revision(draft, changes)
    effective = result["effective_forward_draft"]
    before = {s["scenario_id"]: s for s in draft["scenarios"]}
    after = {s["scenario_id"]: s for s in effective["scenarios"]}
    inputs = []
    for sid, old, new, fields in [(None, draft, effective, COMMON_FIELDS), *[
            (sid, before[sid], after[sid], SCENARIO_FIELDS) for sid in before]]:
        for field in sorted(fields):
            a, b = _value(field, old[field]), _value(field, new[field])
            inputs.append({"scenario_id": sid, "field": field,
                "before": deepcopy(a), "after": deepcopy(b), "value_changed": a != b,
                "rationale_changed": old[field] != new[field] and a == b})
    return {"scope": "MODEL_INPUT_COMPARISON_NOT_FACT_CERTIFICATION", "inputs": inputs}


def render_parameter_facts(record):
    facts = parameter_change_facts(record["forward_draft"], record["forward_revision"]["changes"])
    old = {s["scenario_id"]: s for s in record["forward_draft"]["scenarios"]}
    new = {s["scenario_id"]: s for s in record["effective_forward_draft"]["scenarios"]}
    fields = {r["field"] for r in facts["inputs"] if r["value_changed"] or r["rationale_changed"]}
    # Dividends are independent inputs, not a derived consequence of new EPS.
    fields.add("cash_dividend_per_traded_unit")
    rows = []
    for row in facts["inputs"]:
        sid, field = row["scenario_id"], row["field"]
        if field not in fields:
            continue
        a = record["forward_draft"] if sid is None else old[sid]
        b = record["effective_forward_draft"] if sid is None else new[sid]
        before_label, before = display._input_value(field, a[field], record["forward_draft"])
        after_label, after = display._input_value(field, b[field], record["effective_forward_draft"])
        if field == "noncontrolling_attribution":
            before, after = before_label + "：" + before, after_label + "：" + after
        state = "数值或归属方向已改" if row["value_changed"] else "仅依据或标签改动" if row["rationale_changed"] else "数值未改"
        rows.append((sid or "共同参数", display._PARAMETER_INFO[field][0], before, after, state))
    return ["## 参数前后对账（程序生成）", "",
        "同列属于同一版本，跨情景比较必须在同列进行。未修改的股息不会因盈利修改而自动变化。"
        "下表只证明输入变化，不证明任何新值更合理，也不认证模型申报的会计纠错原因。", "",
        *_table(("情景", "参数", "原版本", "有效版本", "程序核对"), rows)]
