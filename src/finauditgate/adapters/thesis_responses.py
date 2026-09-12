"""Lossless response parsing and exact-input reuse after an interrupted run."""

from copy import deepcopy
import json
from pathlib import Path
import re

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex


def response_candidate(outputs, kind):
    """Merge only disjoint fragments of one schema, never conflicting values."""
    calls = [call for message in outputs for call in message.get("tool_calls", [])]
    if calls:
        merged = {}
        for call in calls:
            args = call.get("args")
            if call.get("name") != kind or not isinstance(args, dict) or not args or set(merged) & set(args):
                raise ValueError("THESIS_RESPONSE_FRAGMENTS_CONFLICT")
            merged.update(args)
        return merged
    if len(outputs) != 1:
        raise ValueError("THESIS_RESPONSE_COUNT_INVALID")
    text = outputs[0]["content"]
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        if kind != "ResearchEvaluation":
            raise
        return _research_markdown(text)
    if (kind == "ResearchEvaluation" and isinstance(candidate, dict)
            and isinstance(candidate.get("plan"), dict)
            and "valuation_basis_and_gaps" in candidate["plan"]):
        if "valuation_basis_and_gaps" in candidate:
            raise ValueError("THESIS_RESEARCH_FIELD_LOCATION_CONFLICT")
        # Observed Flash layout: move this uniquely named field verbatim.
        # Never fill a missing value or choose between competing versions.
        candidate["valuation_basis_and_gaps"] = candidate["plan"].pop("valuation_basis_and_gaps")
    return candidate


def _research_markdown(text):
    """Read the observed manager layout verbatim; no rewriting or default rating."""
    parts = re.split(r"(?m)^### (.+)\n", text)
    headings = parts[1::2]
    expected = ["论点处置", "评级", "理由", "关键失效条件（研究计划触发项）", "估值依据与缺口"]
    if headings != expected:
        raise ValueError("THESIS_RESEARCH_MARKDOWN_LAYOUT_UNSUPPORTED")
    sections = dict(zip(headings, parts[2::2], strict=True))
    rating = re.fullmatch(r"\s*\*\*(Buy|Overweight|Hold|Underweight|Sell|REVIEW)(?:（[^\n*]*）)?\*\*\s*", sections["评级"])
    if rating is None:
        raise ValueError("THESIS_RESEARCH_MARKDOWN_RATING_INVALID")
    rows = []
    for line in sections["论点处置"].splitlines():
        if not line.strip() or line.strip() in ("| 论点 | 处置 | 实质理由 |", "|---|---|---|"):
            continue
        match = re.fullmatch(r"\|\s*\*\*([BS][1-4])\*\*[^|]*\|\s*\*\*(use|conditional|reject)\*\*\s*\| (.+) \|", line)
        if match is None:
            raise ValueError("THESIS_RESEARCH_MARKDOWN_ROW_INVALID")
        rows.append({"claim_id": match[1], "disposition": match[2], "reason": match[3]})
    return {"plan": {"recommendation": rating[1], "rationale": sections["理由"].strip(),
                     "strategic_actions": sections["关键失效条件（研究计划触发项）"].strip()},
            "assessments": rows, "valuation_basis_and_gaps": sections["估值依据与缺口"].strip()}


class CompletedCalls:
    """A bounded completed prefix, reusable only with byte-equivalent messages.

    This does not guess/reconstruct a missing response or bypass a failed
    financial judgment. Original request/response IDs and receipts are kept.
    """

    def __init__(self, root, request, sources, model, *, reassess_final=False):
        self.rows = []
        self.used = 0
        self.receipt = None
        if reassess_final and root is None:
            raise ValueError("THESIS_REASSESS_REQUIRES_RESUME")
        if root is None:
            return
        root = Path(root)
        raw = (root / "runtime-receipt.json").read_bytes()
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("THESIS_RESUME_RECEIPT_TOO_LARGE")
        data = json.loads(raw)
        original = json.loads((root / "request.json").read_bytes())
        if (data.get("schema_version") != "finresearchops.thesis-runtime/v1"
                or original != {"request": request, "sources": sources}):
            raise ValueError("THESIS_RESUME_INPUT_MISMATCH")
        for wire in (root / "model-traces").glob("wire-*.json"):
            if json.loads(wire.read_bytes()).get("model") != model:
                raise ValueError("THESIS_RESUME_MODEL_MISMATCH")
        from finauditgate.adapters.thesis_protocol import schemas
        types = schemas()
        stages = [("Bull Researcher", "InitialBrief"), ("Bear Researcher", "InitialBrief"),
            ("Bull Researcher", "RevisionBrief"), ("Bear Researcher", "RevisionBrief"),
            ("Research Manager", "ResearchEvaluation"), ("Trader", "ExecutionReview"),
            ("Aggressive Analyst", "RiskBrief"), ("Conservative Analyst", "RiskBrief"),
            ("Neutral Analyst", "RiskBrief"), ("Portfolio Manager", "IndependentAssessment"),
            ("Portfolio Manager", "UnderwritingDraft"), ("Portfolio Manager", "FinalAssessment")]
        if reassess_final:
            # Preserve the completed forward proposal, recalculate it, and get
            # a fresh final judgment. Do not redraw assumptions to repair prose.
            stages = stages[:11]
        for row, (node, kind) in zip(data["model_calls"], stages):
            if row.get("error_type") or not row.get("output"):
                break
            if row["node"] != node:
                raise ValueError("THESIS_RESUME_STAGE_ORDER_INVALID")
            try:
                types[kind].model_validate(response_candidate(row["output"], kind))
            except (ValueError, TypeError, KeyError):
                # A returned but incomplete schema is paid evidence, not a
                # completed step. Keep its original receipt; recompute here.
                break
            self.rows.append(deepcopy(row))
        if not self.rows:
            raise ValueError("THESIS_RESUME_NO_COMPLETED_PREFIX")
        self.receipt = {"runtime_sha256": sha256_hex(raw), "available_calls": len(self.rows),
                        "prior_budget": data["budget"], "prior_reuse": data.get("reused_calls")}

    def take(self, node, prompts, capture):
        if self.used == len(self.rows):
            return None
        row = self.rows[self.used]
        actual = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]}
                  for m in row["messages"][0]]
        if row["node"] != node or not any(canonical_json_bytes(actual) == canonical_json_bytes(p) for p in prompts):
            raise ValueError("THESIS_RESUME_MESSAGE_MISMATCH")
        from langchain_core.messages import AIMessage
        if len(row["output"]) != 1:
            raise ValueError("THESIS_RESUME_RESPONSE_INVALID")
        value = row["output"][0]
        message = AIMessage(content=value["content"], id=value["id"], tool_calls=value.get("tool_calls", []),
                            usage_metadata=value.get("usage"), response_metadata={"finish_reason": value.get("finish_reason")})
        capture.model_calls.append(deepcopy(row))
        self.used += 1
        return message, actual
