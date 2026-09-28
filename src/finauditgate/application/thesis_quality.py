"""Replayable, bounded aftercare; the original research Case is never rewritten.

Model review is a proposal, not certification. Exact edits and shared arithmetic
are mechanical checks; substantive support still needs research judgement.
"""

from copy import deepcopy
import json
import re

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.core.forward_revision import apply_forward_revision, COMMON_FIELDS, SCENARIO_FIELDS
from .research_changes import parameter_change_facts
from .research_delivery import contract_for, final_source_view, normalize_report, report_context
from .research_narrative import change_view


SCHEMA = "finresearchops.thesis-review/v3"
IMPACTS = {"wording", "financial", "conclusion", "optional"}
RATINGS = {"Buy", "Hold", "Sell", "REVIEW"}
MEANINGS = {
    "price_only_break_even_pe": "起点价格/情景年度EPS；给定盈利时维持起点价格所需期末PE，单位倍；不是盈利金额或公允倍数。",
    "dividend_adjusted_break_even_pe": "(起点价格-假设期间股息)/情景年度EPS；未计税费的含息打平期末PE，单位倍。",
    "basis_type": "reported等标签也由模型提出，仍需来源核对；analyst_assumption不得在正文升级为披露事实。",
    "cash_proxy": "经营现金流减现金资本购买的代理，不等于可全部分配的股权自由现金流。",
}


def no_answer_length(call):
    """Only an observed length stop with no content, tools or refusal qualifies."""
    failure = call.get("failure_response") or {}
    choices = (failure.get("provider_response") or {}).get("choices") or []
    outputs = call.get("output") or []
    bodies = [c.get("message") or {} for c in choices] + outputs
    length = (failure.get("truncated") is True and "length" in failure.get("finish_reasons", [])) or any(
        o.get("finish_reason") == "length" for o in outputs)
    return bool(length and bodies and not any(str(x.get("content") or "").strip() or x.get("tool_calls") or x.get("refusal") for x in bodies))


def _require(ok, code):
    if not ok:
        raise ValueError("THESIS_QUALITY_" + code)


def _keys(obj, keys):
    _require(isinstance(obj, dict) and set(obj) == set(keys), "SHAPE_INVALID")


def _text(value):
    _require(isinstance(value, str) and bool(value.strip()), "TEXT_REQUIRED")


def _list(value, maximum):
    _require(isinstance(value, list) and len(value) <= maximum, "LIST_INVALID")


def _refs(value, allowed):
    _list(value, 48)
    _require(all(isinstance(v, str) and v in allowed for v in value), "SOURCE_INVALID")


def text_fields(report):
    """An allowlist of existing prose locations, never arbitrary JSON mutation."""
    fields = {"summary.text": report["summary"]["text"],
              "strongest_counterevidence.text": report["strongest_counterevidence"]["text"]}
    fields.update({"financial_analysis." + k + ".text": v["text"] for k, v in report["financial_analysis"].items()})
    for i, row in enumerate(report["scenario_assessments"]):
        for key in ("reason", "what_changes_the_view"):
            fields[f"scenario_assessments.{i}.{key}"] = row[key]
    fields.update({f"limitations.{i}": s for i, s in enumerate(report["limitations"])})
    for key in ("change_explanations", "belief_explanations"):
        fields.update({f"{key}.{i}.explanation.text": row["explanation"]["text"] for i, row in enumerate(report[key])})
    return fields


def _path(path):
    _require(isinstance(path, str), "EDIT_TARGET_INVALID")
    return re.sub(r"\[([0-9]+)\]", r".\1", path)


def reason_targets(record):
    """Only existing assumption reasons; values, types and source data are not prose."""
    targets = {}
    draft = record["effective_forward_draft"]
    for sid, target, prefix, keys in [(None, draft, "effective_forward_draft", COMMON_FIELDS), *[
            (s["scenario_id"], s, f"effective_forward_draft.scenarios.{i}", SCENARIO_FIELDS)
            for i, s in enumerate(draft["scenarios"])]]:
        for field in sorted(keys):
            value = target[field]
            suffix = ".amount.reason" if field == "noncontrolling_attribution" else ".reason"
            assumption = value["amount"] if field == "noncontrolling_attribution" else value
            targets[prefix + "." + field + suffix] = (sid, field, assumption["reason"])
    return targets


def related_text(record, assessment):
    fields = text_fields(record["final_report"])
    targets = reason_targets(record)
    related = {}
    for finding in assessment["findings"]:
        target = targets.get(_path(finding["path"]))
        paths = []
        if target is not None and target[0] is not None:
            paths = [f"scenario_assessments.{i}.reason" for i, s in enumerate(record["final_report"]["scenario_assessments"])
                     if s["scenario_id"] == target[0]]
        elif target is not None:
            paths = ["financial_analysis.valuation_and_price_requirements.text"]
        related[finding["id"]] = {p: fields[p] for p in paths}
    return related


def assumption_context(draft):
    rows = []
    for sid, target, fields in [(None, draft, COMMON_FIELDS), *[
            (s["scenario_id"], s, SCENARIO_FIELDS) for s in draft["scenarios"]]]:
        for key in sorted(fields):
            value = target[key]
            assumption = value["amount"] if key == "noncontrolling_attribution" else value
            rows.append({"scenario_id": sid, "field": key, "basis_type": assumption["basis_type"],
                         "value": deepcopy(value), "is_source_verified": False})
    return rows


def review_payload(record):
    from finauditgate.adapters.thesis_protocol import research_request_view
    return {"request": research_request_view(record["request"]),
        "source_bundle": final_source_view(record["source_bundle"]),
        "report": deepcopy(record["final_report"]),
        "editable_text": text_fields(record["final_report"]),
        "effective_forward_draft": deepcopy(record["effective_forward_draft"]),
        "effective_forward_calculations": deepcopy(record["effective_forward_calculations"]),
        "assumption_context": assumption_context(record["effective_forward_draft"]),
        "metric_meanings": MEANINGS}


def prepare_revision(record, assessment):
    _keys(assessment, {"findings", "financial_changes", "coverage_and_limits"})
    _text(assessment["coverage_and_limits"])
    _list(assessment["findings"], 12)
    _list(assessment["financial_changes"], 12)
    fields = {**text_fields(record["final_report"]), **{p: v[2] for p, v in reason_targets(record).items()}}
    allowed = {s["id"] for s in record["source_bundle"]["sources"] if s.get("use", "research") == "research"}
    findings = {}
    for row in assessment["findings"]:
        _keys(row, {"id", "path", "original_text", "issue", "impact", "evidence_refs", "next_check"})
        _require(isinstance(row["id"], str) and re.fullmatch(r"R[1-9][0-9]?", row["id"]) and row["id"] not in findings, "FINDING_ID_INVALID")
        path = _path(row["path"])
        _require(row["impact"] in IMPACTS and path in fields, "FINDING_TARGET_INVALID")
        for key in ("original_text", "issue", "next_check"):
            _text(row[key])
        _require(fields[path].count(row["original_text"]) == 1, "FINDING_TEXT_MISMATCH")
        _refs(row["evidence_refs"], allowed)
        findings[row["id"]] = row
    changes = []
    for item in assessment["financial_changes"]:
        _keys(item, {"finding_ids", "change"})
        _list(item["finding_ids"], 12)
        _require(bool(item["finding_ids"]) and all(i in findings and findings[i]["impact"] == "financial" for i in item["finding_ids"]), "CHANGE_NOT_JUSTIFIED")
        change = item["change"]
        _refs(change["evidence_refs"], allowed)
        _require(bool(change["evidence_refs"]), "CHANGE_SOURCE_REQUIRED")
        replacement = change["replacement"]
        _refs((replacement["amount"] if change["field"] == "noncontrolling_attribution" else replacement)["evidence_refs"], allowed)
        changes.append(change)
    return apply_forward_revision(record["effective_forward_draft"], changes)


def revision_payload(record, assessment):
    result = prepare_revision(record, assessment)
    payload = review_payload(record)
    payload.update(assessment=deepcopy(assessment), **result)
    payload["assumption_context"] = assumption_context(result["effective_forward_draft"])
    payload["parameter_change_facts"] = parameter_change_facts(record["effective_forward_draft"], [c["change"] for c in assessment["financial_changes"]])
    payload["editable_text"].update({p: v[2] for p, v in reason_targets(record).items()})
    payload["related_text"] = related_text(record, assessment)
    return payload


def _set_text(report, path, value):
    parts = path.split(".")
    target = report
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = value


def apply_revision(record, assessment, revision):
    result = prepare_revision(record, assessment)
    findings = {r["id"]: r for r in assessment["findings"]}
    substantive = {i for i, f in findings.items() if f["impact"] != "optional"}
    if revision is None:
        _require(not substantive and not assessment["financial_changes"], "REVISION_REQUIRED")
        result["final_report"] = deepcopy(record["final_report"])
        unresolved = []
    else:
        _keys(revision, {"edits", "resolutions", "rating", "scenario_decisions"})
        for key, maximum in (("edits", 32), ("resolutions", 12), ("scenario_decisions", 3)):
            _list(revision[key], maximum)
        allowed = {s["id"] for s in record["source_bundle"]["sources"] if s.get("use", "research") == "research"}
        outcomes = {}
        for row in revision["resolutions"]:
            _keys(row, {"finding_id", "outcome", "reason", "evidence_refs"})
            key = row["finding_id"]
            _require(key in findings and key not in outcomes and row["outcome"] in {"corrected", "not_supported", "unresolved"}, "RESOLUTION_INVALID")
            _text(row["reason"]); _refs(row["evidence_refs"], allowed)
            _require(row["outcome"] != "not_supported" or bool(row["evidence_refs"]), "DISAGREEMENT_SOURCE_REQUIRED")
            outcomes[key] = row["outcome"]
        _require(set(outcomes) == set(findings), "RESOLUTION_COVERAGE_INVALID")
        report = deepcopy(record["final_report"])
        targets = reason_targets(record)
        fields = {**text_fields(report), **{p: v[2] for p, v in targets.items()}}
        related = related_text(record, assessment)
        grouped, touched, related_touched = {}, set(), set()
        for edit in revision["edits"]:
            _keys(edit, {"finding_ids", "path", "expected_text", "replacement_text"})
            _list(edit["finding_ids"], 12)
            _require(bool(edit["finding_ids"]) and all(i in substantive and outcomes[i] != "not_supported" for i in edit["finding_ids"]), "EDIT_NOT_JUSTIFIED")
            path = _path(edit["path"])
            _require(path in fields, "EDIT_TARGET_INVALID")
            _text(edit["expected_text"]); _text(edit["replacement_text"])
            whole_related = any(path in related[i] for i in edit["finding_ids"])
            for fid in edit["finding_ids"]:
                finding = findings[fid]
                if path in related[fid]:
                    _require(edit["expected_text"] == fields[path], "EDIT_OUTSIDE_FINDING")
                    related_touched.add(fid)
                    continue
                original = finding["original_text"]
                expected = edit["expected_text"]
                _require(_path(finding["path"]) == path and expected.count(original) == 1, "EDIT_OUTSIDE_FINDING")
                if whole_related:
                    _require(expected == fields[path], "EDIT_OUTSIDE_FINDING")
                    continue
                offset = expected.index(original)
                prefix, suffix = expected[:offset], expected[offset + len(original):]
                replacement = edit["replacement_text"]
                # A model may copy extra surrounding context into expected_text;
                # accept it only when those exact outside bytes stay unchanged.
                _require(replacement.startswith(prefix) and replacement.endswith(suffix)
                         and len(replacement) >= len(prefix) + len(suffix), "EDIT_OUTSIDE_FINDING")
            changed = edit["expected_text"] != edit["replacement_text"]
            _require(fields[path].count(edit["expected_text"]) == 1 and (changed or whole_related), "EDIT_TEXT_MISMATCH")
            # Whole sections cannot silently disappear; only anchored spans change.
            start = fields[path].index(edit["expected_text"])
            grouped.setdefault(path, []).append((start, start + len(edit["expected_text"]), edit["replacement_text"]))
            if changed:
                touched.update(i for i in edit["finding_ids"] if _path(findings[i]["path"]) == path and findings[i]["original_text"] in edit["expected_text"])
        reason_changes = []
        explicit_targets = {(c["change"]["scenario_id"], c["change"]["field"]) for c in assessment["financial_changes"]}
        for path, spans in grouped.items():
            ordered = sorted(spans)
            _require(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), "OVERLAPPING_EDITS")
            value = fields[path]
            for start, end, replacement in reversed(ordered):
                value = value[:start] + replacement + value[end:]
            # A conservative loss-of-content guard, not a semantic score. Leave
            # the original intact when a proposed edit collapses the analysis.
            prose = lambda text: re.sub(r"\{\{[^{}]+\}\}|\s+", "", text)
            old_length, new_length = len(prose(fields[path])), len(prose(value))
            minimum = min(old_length, max(20, (old_length + 1) // 2))
            _require(new_length >= minimum, "RESEARCH_CONTENT_COLLAPSED")
            if path not in targets:
                _set_text(report, path, value)
                continue
            sid, field, _ = targets[path]
            _require((sid, field) not in explicit_targets, "REASON_ALREADY_REVISED")
            old = record["effective_forward_draft"] if sid is None else next(s for s in record["effective_forward_draft"]["scenarios"] if s["scenario_id"] == sid)
            replacement = deepcopy(old[field])
            assumption = replacement["amount"] if field == "noncontrolling_attribution" else replacement
            assumption["reason"] = value
            ids = [i for i, f in findings.items() if _path(f["path"]) == path]
            refs = list(dict.fromkeys([*assumption["evidence_refs"], *[ref for i in ids for ref in findings[i]["evidence_refs"]]]))
            assumption["evidence_refs"] = refs
            expected = {"nature": old[field]["nature"], "amount": old[field]["amount"]["value"]} if field == "noncontrolling_attribution" else old[field]["value"]
            reason_changes.append({"scenario_id": sid, "field": field, "expected_before": expected,
                "replacement": replacement, "correction_basis": "assumption_update",
                "reason": "只更正假设理由；数值、性质和依据类型不变。", "evidence_refs": refs})
        if reason_changes:
            result = apply_forward_revision(record["effective_forward_draft"], [c["change"] for c in assessment["financial_changes"]] + reason_changes)
        changed_findings = {i for c in assessment["financial_changes"] for i in c["finding_ids"]}
        _require(all(outcomes[i] == "corrected" for i in changed_findings), "UNACCEPTED_FINANCIAL_CHANGE")
        _require(all(outcomes[i] != "corrected" or i in touched for i in substantive), "CORRECTION_NOT_APPLIED")
        # Known unresolved material claims must be qualified in their own body,
        # never hidden by adding only an appendix disclaimer.
        _require(all(outcomes[i] != "unresolved" or i in touched for i in substantive), "UNRESOLVED_BODY_NOT_CHANGED")
        _require(all(i in related_touched for i in substantive if _path(findings[i]["path"]) in targets
                     and outcomes[i] in {"corrected", "unresolved"}), "REASON_BODY_NOT_REVIEWED")
        _keys(revision["rating"], {"value", "reason"})
        _text(revision["rating"]["reason"])
        rating = revision["rating"]["value"]
        _require(rating in RATINGS, "RATING_INVALID")
        material = any(i in substantive and i in touched and
                       (f["impact"] in {"financial", "conclusion"} or _path(f["path"]) in targets)
                       and outcomes[i] != "not_supported" for i, f in findings.items())
        _require(rating == report["rating"] or material, "UNJUSTIFIED_RATING_CHANGE")
        report["rating"] = rating
        scenarios = {s["scenario_id"]: s for s in report["scenario_assessments"]}
        seen = set()
        for item in revision["scenario_decisions"]:
            _keys(item, {"scenario_id", "disposition"})
            sid = item["scenario_id"]
            _require(material and sid in scenarios and sid not in seen and item["disposition"] in {"use", "conditional", "reject"}, "SCENARIO_DECISION_INVALID")
            scenarios[sid]["disposition"] = item["disposition"]; seen.add(sid)
        result["final_report"] = normalize_report(report, record["source_bundle"])
        unresolved = [i for i in findings if i in substantive and outcomes[i] == "unresolved"]
    # All financial quantities still pass the same numeric/source contracts.
    context = report_context(result["final_report"], result["effective_forward_draft"], result["effective_forward_calculations"],
        record["source_bundle"], record["request"], changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"],
        contract=contract_for(record))
    _reason_prose(record, revision, result, context)
    result["evidence_check"] = context.evidence_check()
    result["unresolved_findings"] = unresolved
    result["status"] = "PARTIAL" if unresolved or context.evidence_check()["findings"] else "COMPLETED"
    return result


def _reason_prose(record, revision, effective, context):
    """Validate and render only newly edited reasons with the same body rules."""
    if revision is None:
        return []
    old = reason_targets(record)
    display_record = {**record, "effective_forward_draft": effective["effective_forward_draft"]}
    new = reason_targets(display_record)
    rows = []
    for path in dict.fromkeys(_path(e["path"]) for e in revision["edits"] if _path(e["path"]) in old):
        context._record_findings = True
        context._field = path
        rows.append((new[path][0], new[path][1], context.text(new[path][2])))
    context._record_findings = False
    context._field = ""
    return rows


def validate(record, review):
    """Rebuild the sidecar from actual model requests/responses using stdlib."""
    from finauditgate.adapters.thesis_responses import response_candidate
    _require(review.get("schema_version") == SCHEMA and review.get("main_sha256") == sha256_hex(canonical_json_bytes(record)), "MAIN_BINDING_INVALID")
    _require(review.get("status") in {"COMPLETED", "PARTIAL"}, "STATUS_INVALID")
    calls, exchanges = review["model_calls"], review["exchanges"]
    _list(calls, 4); _list(exchanges, 2)
    _require(all(isinstance(e, dict) for e in exchanges) and all(isinstance(c, dict) for c in calls), "CALL_INVALID")
    _require([e["kind"] for e in exchanges] in ([], ["QualityReview"], ["QualityReview", "QualityRevision"]), "ORDER_INVALID")
    parsed, order, retries, stages = [], [], 0, {}
    for i, call in enumerate(calls):
        tag = call.get("thesis_stage") or {}
        _require(call.get("node") == "Data Review Agent" and tag.get("kind") in {"QualityReview", "QualityRevision"}, "CALL_STAGE_INVALID")
        _require(type(tag.get("attempt")) is int and tag.get("attempt") in (1, 2)
                 and tag.get("reasoning_effort") == ("max" if tag.get("attempt") == 1 else "high"), "CALL_STAGE_INVALID")
        stages.setdefault(tag["kind"], []).append(tag["attempt"])
        _require(stages[tag["kind"]] in ([1], [1, 2]), "CALL_STAGE_INVALID")
        if tag["attempt"] == 2:
            retries += 1
            _require(i > 0 and no_answer_length(calls[i-1]) and calls[i-1]["thesis_stage"] == {
                "kind": tag["kind"], "attempt": 1, "reasoning_effort": "max"}
                and calls[i-1]["messages"] == call["messages"], "RETRY_NOT_PROVEN")
    from finauditgate.adapters.thesis_recovery import extra_call_limit
    _require(retries + len(record["recovery"]["attempts"]) <= extra_call_limit(record["recovery"]["policy"]), "RETRY_LIMIT")
    for i, exchange in enumerate(exchanges):
        from finauditgate.adapters.thesis_recovery import successful_call
        matches = [(index, c) for index, c in enumerate(calls) if any(o.get("id") == exchange["response_id"] for o in c.get("output", []))]
        _require(len(matches) == 1, "CALL_BINDING_INVALID")
        index, call = matches[0]
        _require(successful_call(call) and call["thesis_stage"]["kind"] == exchange["kind"], "CALL_INVALID")
        order.append(index)
        batches = call.get("messages")
        _require(isinstance(batches, list) and len(batches) == 1 and isinstance(batches[0], list)
                 and len(batches[0]) == 2 and all(isinstance(m, dict) for m in batches[0]), "CALL_MESSAGES_INVALID")
        messages = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]} for m in call["messages"][0]]
        _require(exchange["messages"] == messages and any(o.get("id") == exchange["response_id"] for o in call.get("output", [])), "CALL_BINDING_INVALID")
        value = response_candidate(call["output"], exchange["kind"], protocol_version=16)
        _require(value == exchange["parsed"], "OUTPUT_CHANGED")
        expected = review_payload(record) if i == 0 else revision_payload(record, parsed[0])
        _require(json.loads(messages[1]["content"]) == {"node": "Data Review Agent", **expected}, "INPUT_CHANGED")
        parsed.append(value)
    _require(order == sorted(set(order)), "ORDER_INVALID")
    _require(review.get("assessment") == (parsed[0] if parsed else None), "ASSESSMENT_CHANGED")
    _require(review.get("revision") == (parsed[1] if len(parsed) > 1 else None), "REVISION_CHANGED")
    _require(review.get("findings") == (parsed[0]["findings"] if parsed else []), "FINDINGS_CHANGED")
    if parsed:
        _require(review.get("coverage_and_limits") == parsed[0]["coverage_and_limits"], "COVERAGE_CHANGED")
    if review["effective"] is not None:
        _require(bool(parsed), "ASSESSMENT_REQUIRED")
        _require(set(order) == {i for i, c in enumerate(calls) if successful_call(c) and c.get("output")}, "UNBOUND_ANSWER")
        expected = apply_revision(record, parsed[0], parsed[1] if len(parsed) > 1 else None)
        _require(review["effective"] == expected and review["status"] == expected["status"], "EFFECTIVE_CHANGED")
    else:
        _require(review["status"] == "PARTIAL", "FALSE_COMPLETION")


def render_report(record, review):
    from .thesis_report_v13 import render_with_context
    from .forward_report import _table
    from .thesis_report_v11 import _input_value, _PARAMETER_INFO
    effective = review["effective"]
    if effective is None:
        return None
    display = deepcopy(record)
    display.update({k: deepcopy(effective[k]) for k in ("final_report", "effective_forward_draft", "effective_forward_calculations", "evidence_check")})
    context = report_context(display["final_report"], display["effective_forward_draft"], display["effective_forward_calculations"],
        display["source_bundle"], display["request"], changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"],
        contract=contract_for(record))
    reason_rows = _reason_prose(record, review["revision"], effective, context)
    # The old before/after tables belong to the original chain. Display this
    # aftercare's effective-to-effective change, while preserving original Case.
    display["forward_draft"] = deepcopy(record["effective_forward_draft"])
    display["forward_revision"]["changes"] = [{"scenario_id": a["scenario_id"], "field": a["field"],
        "expected_before": a["expected_before"], "replacement": a["after"], "correction_basis": a["correction_basis"],
        "reason": a["reason"], "evidence_refs": a["evidence_refs"]} for a in effective["applied_changes"]]
    display["applied_changes"] = deepcopy(effective["applied_changes"])
    # The shared renderer needs selector rows for its change tables. These
    # contain no model explanation or invented number; the actual proposed
    # rationale remains in the separately bound aftercare record.
    display["final_report"]["change_explanations"] = [
        {"scenario_id": r["scenario_id"], "field": r["field"], "explanation": {
            "text": "本次校订的字段变化与模型理由见校订记录。", "evidence_refs": r["evidence_refs"], "metrics": []}}
        for r in effective["applied_changes"]]
    body = render_with_context(display, context, evidence_check=effective["evidence_check"], mechanical_changes=True).decode()
    if reason_rows:
        body += "\n\n## 本次更正的假设依据\n\n数值未因此改变；以下是模型更正后的理由，仍须核对来源和适用范围。\n\n"
        for sid, field, prose in reason_rows:
            body += f"### {sid or '共同参数'} · {_PARAMETER_INFO[field][0]}\n\n{prose}\n\n"
    body = body.replace("(process-record.md)", "(quality-process-record.md)")
    # Keep every cited source fragment, but display identical repetitions once.
    footnotes = []
    for qid in sorted(context.quotes):
        excerpt = context.source(qid)
        if excerpt in body:
            body = body.replace(excerpt, "[^" + qid + "]")
            footnotes.append("[^" + qid + "]: " + excerpt)
    rows = []
    for item in assumption_context(effective["effective_forward_draft"]):
        label, value = _input_value(item["field"], item["value"], effective["effective_forward_draft"])
        basis = {"reported": "模型标注披露值，仍须核对", "company_guidance": "模型标注公司指引，仍须核对",
                 "reference_comparison": "参考比较", "analyst_assumption": "分析假设，非披露事实"}[item["basis_type"]]
        rows.append((item["scenario_id"] or "共同", label, value, basis))
    warning = ("存在未解决的实质疑点；下列模型评级不能视为可用的已核结论。" if effective["status"] == "PARTIAL" else
               "模型复核与校订已完成，机械检查不证明经济判断正确；尚需人工审阅。")
    degraded = record.get("recovery", {}).get("degraded", [])
    if degraded:
        warning += "本次有" + str(len(degraded)) + "个非关键角色按降级规则省略，原Case状态为PARTIAL。"
    header = "> 自动校订稿；" + warning + "原稿完整保留为[原始报告](report.md)。\n\n"
    meanings = ["", "## 参数性质与计算含义（程序提供）", "", *_table(("情景", "参数", "取值", "依据性质"), rows), "",
                *["- " + value for value in MEANINGS.values()]]
    rendered = (header + body + "\n".join(meanings) + "\n\n## 引用原文\n\n" + "\n\n".join(footnotes)).encode()
    if record["schema_version"] in ("finresearchops.thesis-case/v17", "finresearchops.thesis-case/v18", "finresearchops.thesis-case/v19", "finresearchops.thesis-case/v20"):
        from .thesis_report_v17 import supplement
        rendered += supplement(display)
    return rendered


def render_process(record, review):
    if record["schema_version"] in ("finresearchops.thesis-case/v17", "finresearchops.thesis-case/v18", "finresearchops.thesis-case/v19", "finresearchops.thesis-case/v20"):
        from .thesis_report_v17 import render_process as original_process
    else:
        from .thesis_report_v16 import render_process as original_process
    return original_process(record) + ("\n\n## 本次自动校订与未决问题\n\n以下为模型复核提案及逐项处理，不是金融认证。\n\n```json\n"
        + json.dumps({k: review.get(k) for k in ("assessment", "revision", "effective")}, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n").encode()
