"""Protocol 20: rewrite, once, only the final-report sentences the numeric contract refuses.

The contract itself is unchanged. A refusal is repairable only when it is
local to at most MAX_SENTENCES whole sentences; the model returns a
replacement for each exact sentence, the program splices them in, and the
complete report is checked again. Everything else in the report is frozen.
"""

from copy import deepcopy
import json
import re

from finauditgate.core.artifacts import canonical_json_bytes


MAX_SENTENCES = 5
KIND = "FinalNumberRepair"
_PLACEHOLDER = "此句待改写。"  # used only while locating refusals; never saved
# Frozen schema of the repair answer; integration tests require the live model to match it.
SCHEMA = {
    "additionalProperties": False,
    "properties": {"replacements": {"items": {"$ref": "#/$defs/Replacement"}, "maxItems": MAX_SENTENCES, "minItems": 1,
                                    "title": "Replacements", "type": "array"}},
    "required": ["replacements"], "title": "FinalNumberRepair", "type": "object",
    "$defs": {"Replacement": {
        "additionalProperties": False,
        "properties": {"field": {"title": "Field", "type": "string"},
                       "original": {"minLength": 1, "title": "Original", "type": "string"},
                       "replacement": {"minLength": 1, "title": "Replacement", "type": "string"}},
        "required": ["field", "original", "replacement"], "title": "Replacement", "type": "object"}},
}
# A line break is its own unit, so a quoted sentence never has to carry one.
_SENTENCE = re.compile(r"[^。！？!?\n]*[。！？!?]+|[^。！？!?\n]+|\n")
_PATH = re.compile(r"(summary|strongest_counterevidence)|financial_analysis\.([a-z_]+)"
                   r"|(scenario_assessments)\[([0-9]+)\]\.(reason|what_changes_the_view)"
                   r"|(limitations)\[([0-9]+)\]|(change_explanations|belief_explanations)\[([0-9]+)\]\.explanation")


def repair_type():
    from pydantic import BaseModel, ConfigDict, Field

    class Replacement(BaseModel):
        model_config = ConfigDict(extra="forbid")
        field: str
        original: str = Field(min_length=1)
        replacement: str = Field(min_length=1)

    class FinalNumberRepair(BaseModel):
        model_config = ConfigDict(extra="forbid")
        replacements: list[Replacement] = Field(min_length=1, max_length=MAX_SENTENCES)

    return FinalNumberRepair


def final_texts(report):
    """(field path, prose) of every final-report text the numeric contract reads, in its order."""
    rows = [("summary", report["summary"]["text"])]
    rows += [("financial_analysis." + name, value["text"]) for name, value in sorted(report["financial_analysis"].items())]
    rows.append(("strongest_counterevidence", report["strongest_counterevidence"]["text"]))
    for i, row in enumerate(report["scenario_assessments"]):
        rows += [(f"scenario_assessments[{i}].reason", row["reason"]),
                 (f"scenario_assessments[{i}].what_changes_the_view", row["what_changes_the_view"])]
    rows += [(f"limitations[{i}]", value) for i, value in enumerate(report["limitations"])]
    for key in ("change_explanations", "belief_explanations"):
        rows += [(f"{key}[{i}].explanation", row["explanation"]["text"]) for i, row in enumerate(report[key])]
    return rows


def _set(report, field, text):
    m = _PATH.fullmatch(field)
    if m is None:
        raise ValueError("THESIS_NUMBER_REPAIR_FIELD_INVALID")
    if m[1]:
        report[m[1]]["text"] = text
    elif m[2]:
        report["financial_analysis"][m[2]]["text"] = text
    elif m[3]:
        report[m[3]][int(m[4])][m[5]] = text
    elif m[6]:
        report[m[6]][int(m[7])] = text
    else:
        report[m[8]][int(m[9])]["explanation"]["text"] = text


def sentences(text):
    """Split prose into sentences whose concatenation is exactly the text."""
    parts = _SENTENCE.findall(text)
    if "".join(parts) != text:
        raise ValueError("THESIS_NUMBER_REPAIR_SPLIT_INVALID")
    return parts


def refused_sentences(report, draft, calculations, sources, request, *, changes, beliefs):
    """The sentences number contract 2 refuses, as [{field, sentence}]; None when not repairable.

    The complete report must fail the unchanged check with UNBOUND_RESEARCH_NUMBER.
    Each candidate sentence is first found on its own, then confirmed in its
    context: with every candidate replaced by a neutral placeholder the report
    must pass, and a candidate is kept only when restoring it alone brings the
    refusal back. A repair is therefore never requested for a report that has
    another contract error or a refusal outside the listed sentences.
    """
    from finauditgate.application.research_delivery import DeliveryContext, report_context
    from finauditgate.application.research_narrative import _TOKEN

    def check(value):
        report_context(value, draft, calculations, sources, request, changes=changes, beliefs=beliefs, contract=2)

    try:
        check(report)
    except ValueError as exc:
        if str(exc) != "UNBOUND_RESEARCH_NUMBER":
            return None
    else:
        return None
    context = DeliveryContext(draft, calculations, sources, report, request, contract=2)
    found = []
    for field, text in final_texts(report):
        parts = sentences(text)
        for sentence in parts:
            for segment in _TOKEN.split(sentence)[::2]:
                try:
                    context.unbound_numbers(segment)
                except ValueError as exc:
                    if (str(exc) != "UNBOUND_RESEARCH_NUMBER"
                            or sum(part.strip() == sentence.strip() for part in parts) != 1):
                        return None
                    found.append({"field": field, "sentence": sentence})
                    break
    if not 0 < len(found) <= MAX_SENTENCES:
        return None

    def placed(keep=None):
        value = deepcopy(report)
        texts = dict(final_texts(value))
        listed = {(r["field"], r["sentence"]) for r in found if (r["field"], r["sentence"]) != keep}
        for field in dict.fromkeys(r["field"] for r in found):
            _set(value, field, "".join(_PLACEHOLDER if (field, part) in listed else part for part in sentences(texts[field])))
        return value

    try:
        check(placed())
    except ValueError:
        return None
    kept = []
    for row in found:
        try:
            check(placed((row["field"], row["sentence"])))
        except ValueError as exc:
            if str(exc) != "UNBOUND_RESEARCH_NUMBER":
                return None
            kept.append(row)
    return kept or None


def repair_messages(base, refused):
    prompt = deepcopy(base)
    prompt[0]["content"] += (
        "\n【本次任务变更】上文终稿已经生成。本次不重写终稿，也不返回FinalResearchReport，"
        "只改写下列被数字规则拒收的句子。每一项的original必须原样照抄该句，replacement给出改写后的整句。"
        "改写句不得出现金额、比率、数量、倍数、股数等数字（阿拉伯数字或中文数字都不行），日期可写成“2025年第三季度”这种形式；"
        "需要数量时改用资料中已给的{{source:E....}}证据块或{{metric:...}}引用，或改为定性描述。"
        "保留原句的判断与限定，不新增结论，不改动评级、情景采纳和任何其他句子。"
        "只返回一个符合下面FinalNumberRepair JSON Schema的对象，每个被拒句恰好一项。字段路径与被拒原句：\n"
        + canonical_json_bytes({"refused_sentences": refused}).decode()
        + "\nJSON Schema：\n" + canonical_json_bytes(SCHEMA).decode())
    return prompt


def parse_replacements(outputs):
    """Replacements from a saved repair answer, checked against the frozen schema; ValueError otherwise."""
    from .thesis_format import _errors
    if len(outputs) != 1 or outputs[0].get("tool_calls"):
        raise ValueError("THESIS_NUMBER_REPAIR_INVALID")
    value = json.loads(outputs[0]["content"])
    if _errors(value, SCHEMA, SCHEMA["$defs"], ()):
        raise ValueError("THESIS_NUMBER_REPAIR_INVALID")
    return value["replacements"]


def _swap(part, replacement):
    """The replacement keeps the whitespace that surrounded the refused sentence."""
    core = part.strip()
    start = part.index(core)
    return part[:start] + replacement.strip() + part[start + len(core):]


def apply_replacements(report, refused, replacements):
    """Replace exactly the refused sentences, one replacement each; any other change is refused.

    An original matches its refused sentence when both are equal after removing
    surrounding whitespace; the refused sentences are unique in that form.
    """
    wanted = [(r["field"], r["sentence"].strip()) for r in refused]
    given = [(r["field"], r["original"].strip()) for r in replacements]
    if sorted(given) != sorted(wanted) or len(set(given)) != len(given):
        raise ValueError("THESIS_NUMBER_REPAIR_MISMATCH")
    if any(not isinstance(r["replacement"], str) or not r["replacement"].strip() for r in replacements):
        raise ValueError("THESIS_NUMBER_REPAIR_MISMATCH")
    repaired = deepcopy(report)
    texts = dict(final_texts(repaired))
    swaps = {(r["field"], r["original"].strip()): r["replacement"] for r in replacements}
    for field in dict.fromkeys(r["field"] for r in refused):
        _set(repaired, field, "".join(_swap(part, swaps[field, part.strip()]) if (field, part.strip()) in swaps else part
                                      for part in sentences(texts[field])))
    return repaired


def repaired_report(base_outputs, refused, replacements, protocol_version, bundle):
    """The final-report candidate with the replacements spliced in, and its normalized form."""
    from finauditgate.application.research_delivery import normalize_report
    from .thesis_format import schema_errors
    from .thesis_responses import response_candidate
    base = response_candidate(base_outputs, "FinalResearchReport", protocol_version=protocol_version)
    candidate = apply_replacements(base, refused, replacements)
    if schema_errors("FinalResearchReport", candidate):
        raise ValueError("THESIS_NUMBER_REPAIR_SCHEMA_INVALID")
    return candidate, normalize_report(candidate, bundle)
