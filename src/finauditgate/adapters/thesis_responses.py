"""Lossless response parsing and exact-input reuse after an interrupted run."""

from copy import deepcopy
import json
from pathlib import Path
import re

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex


def response_candidate(outputs, kind, *, protocol_version=13):
    """Merge only disjoint fragments of one schema, never conflicting values."""
    calls = [call for message in outputs for call in message.get("tool_calls", [])]
    if calls:
        merged = {}
        for call in calls:
            args = call.get("args")
            if call.get("name") != kind or not isinstance(args, dict) or not args or set(merged) & set(args):
                raise ValueError("THESIS_RESPONSE_FRAGMENTS_CONFLICT")
            merged.update(args)
        if protocol_version >= 16:
            from .thesis_recovery import normalize_role
            merged = normalize_role(merged, kind)
        if protocol_version >= 18:
            from .thesis_format import normalize_format
            merged = normalize_format(merged, kind, protocol_version)
        return merged
    if len(outputs) != 1:
        raise ValueError("THESIS_RESPONSE_COUNT_INVALID")
    text = outputs[0]["content"]
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        from .thesis_recovery import close_json_tail
        repaired = close_json_tail(text) if protocol_version >= 16 else None
        if repaired is not None:
            candidate = json.loads(repaired)
        elif kind != "ResearchEvaluation":
            raise
        else:
            return _research_markdown(text)
    if (kind == "ResearchEvaluation" and isinstance(candidate, dict)
            and isinstance(candidate.get("plan"), dict)
            and "valuation_basis_and_gaps" in candidate["plan"]):
        if "valuation_basis_and_gaps" in candidate:
            raise ValueError("THESIS_RESEARCH_FIELD_LOCATION_CONFLICT")
        # Observed Flash layout: move this uniquely named field verbatim.
        # Never fill a missing value or choose between competing versions.
        candidate["valuation_basis_and_gaps"] = candidate["plan"].pop("valuation_basis_and_gaps")
    if kind == "FinalResearchReport" and protocol_version < 14:
        echoes = scenario_metric_echoes(candidate)
        for row in candidate.get("scenario_assessments", []) if echoes else []:
            row.pop("metrics", None)
    if protocol_version >= 16:
        from .thesis_recovery import normalize_role
        candidate = normalize_role(candidate, kind)
    if kind == "AnalystReport":
        from .thesis_analysts import normalize_limits
        candidate = normalize_limits(candidate)
    if protocol_version >= 18:
        from .thesis_format import normalize_format
        candidate = normalize_format(candidate, kind, protocol_version)
    return candidate


def scenario_metric_echoes(value):
    """Recognize only redundant v13 selector metadata already present inline.

    No values, labels, prose, rating or novel references are removed. Original
    provider output remains in model_calls; the process appendix records echoes.
    """
    if not isinstance(value, dict) or not {"source_quotes", "change_explanations", "belief_explanations"} <= value.keys():
        return []
    from finauditgate.application.research_numbers import METRIC_KEYS
    rows = value.get("scenario_assessments")
    if not isinstance(rows, list):
        return []
    echoes = []
    for row in rows:
        if not isinstance(row, dict):
            return []
        if "metrics" not in row:
            continue
        refs = row["metrics"]
        reason, trigger = row.get("reason"), row.get("what_changes_the_view")
        if not isinstance(refs, list) or len(refs) > 6 or not isinstance(reason, str) or not isinstance(trigger, str):
            raise ValueError("THESIS_SCENARIO_METRIC_ECHO_CONFLICT")
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"scenario_id", "metric"}
                    or not isinstance(ref["scenario_id"], str) or ref["scenario_id"] not in {"F1", "F2", "F3"}
                    or ref["scenario_id"] != row.get("scenario_id")
                    or not isinstance(ref["metric"], str) or ref["metric"] not in METRIC_KEYS):
                raise ValueError("THESIS_SCENARIO_METRIC_ECHO_CONFLICT")
            token = "{{metric:" + ref["scenario_id"] + ":" + ref["metric"] + "}}"
            if token not in reason and token not in trigger:
                raise ValueError("THESIS_SCENARIO_METRIC_ECHO_CONFLICT")
        echoes.append({"scenario_id": row.get("scenario_id"), "metrics": deepcopy(refs)})
    return echoes


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

    def __init__(self, root, request, sources, model, *, reassess_final=False, protocol_version=10,
                 replay_presentation_failure=False):
        if protocol_version not in (10, 11, 13, 16, 17, 18, 19):
            raise ValueError("THESIS_PROTOCOL_VERSION_INVALID")
        self.rows = []
        self.used = 0
        self.receipt = None
        self.recovery_state = None
        self.budget_checkpoint = None
        self._recovery_dependencies = {}
        self._prior_calls = []
        self.current_runtime = False
        if replay_presentation_failure and (root is None or reassess_final or protocol_version not in (16, 17)):
            raise ValueError("THESIS_PRESENTATION_REPLAY_OPTIONS_INVALID")
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
        current = data.get("schema_version") == "finresearchops.thesis-runtime/v3"
        self.current_runtime = current
        if (data.get("schema_version") not in ("finresearchops.thesis-runtime/v1", "finresearchops.thesis-runtime/v3")
                or (current and (protocol_version not in (16, 17, 18, 19) or data.get("protocol_version") != protocol_version))
                or original != {"request": request, "sources": sources}):
            raise ValueError("THESIS_RESUME_INPUT_MISMATCH")
        excluded = set()
        if current:
            from .thesis_recovery import validate_budget_reservations, validate_recoveries
            self.recovery_state = deepcopy(data["recovery"])
            self.budget_checkpoint = deepcopy(data["budget_checkpoint"])
            if ((self.budget_checkpoint is None) != (data["budget"] is None)
                    or (self.budget_checkpoint is not None and self.budget_checkpoint.get("receipt") != data["budget"])):
                raise ValueError("THESIS_RESUME_BUDGET_BINDING_INVALID")
            excluded, self._recovery_dependencies = validate_recoveries(data["model_calls"], self.recovery_state,
                                                                        protocol_version=protocol_version)
            validate_budget_reservations(self.budget_checkpoint, data["model_calls"], self.recovery_state, data.get("reused_calls"))
            self._prior_calls = deepcopy(data["model_calls"])
            if reassess_final and self.recovery_state["halted"] is None and not self.recovery_state["retired_final_calls"]:
                entries = [a for a in self.recovery_state["attempts"] if a["kind"] == "FinalResearchReport"]
                ids = list(dict.fromkeys(i for a in entries for i in (a["failed_run_id"], a["retry_run_id"])))
                by_id = {c["run_id"]: c for c in data["model_calls"]}
                self.recovery_state["retired_final_calls"] = [deepcopy(by_id[i]) for i in ids]
        for wire in (root / "model-traces").glob("wire-*.json"):
            if json.loads(wire.read_bytes()).get("model") != model:
                raise ValueError("THESIS_RESUME_MODEL_MISMATCH")
        from finauditgate.adapters.thesis_protocol import schemas
        types = schemas()
        if protocol_version >= 11:
            from finauditgate.adapters.thesis_correction import correction_schemas
            types.update(correction_schemas(types, bound=protocol_version >= 13, selected=protocol_version >= 14))
        from .thesis_analysts import main_stages
        stages = main_stages(protocol_version)
        if protocol_version >= 17:
            from .thesis_analysts import schemas as analyst_schemas
            types.update(analyst_schemas())
        if reassess_final:
            # Preserve the completed forward proposal, recalculate it, and get
            # a fresh final judgment. Do not redraw assumptions to repair prose.
            stages = stages[:-1]
        prefix_stop = None
        # Failed attempts remain in their original receipt, never in reused outputs.
        from .thesis_recovery import successful_call
        successful = (r for r in data["model_calls"] if (successful_call(r) if current else not r.get("error_type")) and r.get("run_id") not in excluded)
        for row, (node, kind) in zip(successful, stages):
            if not row.get("output"):
                break
            if row["node"] != node:
                raise ValueError("THESIS_RESUME_STAGE_ORDER_INVALID")
            if current:
                from .thesis_recovery import validate_stage_tag
                validate_stage_tag(row, kind, self._recovery_dependencies)
            try:
                parsed = types[kind].model_validate(response_candidate(row["output"], kind,
                    protocol_version=protocol_version)).model_dump(mode="json")
                if protocol_version >= 11:
                    from finauditgate.adapters.thesis_protocol import check_refs, source_view
                    selected = protocol_version >= 14 and kind == "FinalResearchReport"
                    if selected:
                        from finauditgate.application.research_delivery import normalize_report, final_source_view
                        parsed = normalize_report(parsed, sources)
                    else:
                        check_refs(parsed, {s["id"] for s in source_view(sources)["sources"]})
                    payload = json.loads(row["messages"][0][1]["content"])
                    if selected and payload["source_bundle"] != final_source_view(sources):
                        raise ValueError("THESIS_EVIDENCE_CATALOG_BINDING_INVALID")
                    if kind == "ForwardRevision":
                        from finauditgate.core.forward_revision import apply_forward_revision
                        revised = apply_forward_revision(payload["forward_draft"], parsed["changes"])
                        for key, field, expected in (
                                ("claim_assessments", "claim_id", [c["id"] for c in payload["updated_claims"]]),
                                ("belief_updates", "belief_id", [b["belief_id"] for b in payload["independent_beliefs"]])):
                            if len(parsed[key]) != len(expected) or {r[field] for r in parsed[key]} != set(expected):
                                raise ValueError("THESIS_RESUME_INCOMPLETE_REVISION")
                        from .thesis_correction import valid_belief_updates
                        if not valid_belief_updates(parsed["belief_updates"], allow_maintain_restatement=protocol_version >= 17):
                            raise ValueError("THESIS_RESUME_INCOMPLETE_REVISION")
                    if kind == "FinalResearchReport":
                        from finauditgate.application.research_numbers import render_research_block
                        for block in (parsed["summary"], *parsed["financial_analysis"].values(), parsed["strongest_counterevidence"]):
                            render_research_block(block, payload["effective_forward_draft"], payload["effective_forward_calculations"])
                        if protocol_version >= 13:
                            from finauditgate.application.research_narrative import report_context
                            if selected:
                                from finauditgate.application.research_delivery import report_context
                            report_context(parsed, payload["effective_forward_draft"], payload["effective_forward_calculations"], sources, request,
                                changes=payload["change_context"], beliefs=payload["research_resolution"]["belief_updates"],
                                **({"contract": 2} if protocol_version >= 18 else {}))
                        expected = [s["scenario_id"] for s in payload["effective_forward_draft"]["scenarios"]]
                        given = parsed["scenario_assessments"]
                        if len(given) != len(expected) or {r["scenario_id"] for r in given} != set(expected):
                            raise ValueError("THESIS_RESUME_INCOMPLETE_REPORT")
            except (ValueError, TypeError, KeyError) as exc:
                # A returned but incomplete schema is paid evidence, not a
                # completed step. Keep its original receipt; recompute here.
                code = str(exc)
                prefix_stop = {"node": node, "kind": kind,
                               "error_code": code if re.fullmatch(r"[A-Z0-9_]{1,120}", code) else type(exc).__name__}
                break
            self.rows.append(deepcopy(row))
        if not self.rows and not current:
            raise ValueError("THESIS_RESUME_NO_COMPLETED_PREFIX")
        self.receipt = {"runtime_sha256": sha256_hex(raw), "available_calls": len(self.rows),
                        "prior_budget": data["budget"], "prior_reuse": data.get("reused_calls")}
        if replay_presentation_failure:
            if protocol_version == 17:
                from .thesis_analysts import ANALYSTS, normalize_limits
                from .thesis_recovery import NOTE_KINDS, normalize_role
                halt = self.recovery_state.get("halted") if self.recovery_state else None
                expected_halt = halt
                if not current or not isinstance(halt, dict) or prefix_stop is not None or not self.rows:
                    raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                captured = self.rows[-1]
                if len(captured.get("output", [])) != 1 or captured["output"][0].get("tool_calls"):
                    raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                content = captured["output"][0]["content"]
                try:
                    original_value = json.loads(content)
                except json.JSONDecodeError:
                    original_value = None
                new_primary_calls_allowed = True
                if original_value is None:
                    # Only the missing trailing closers are supplied; the answer text is unchanged.
                    from .thesis_recovery import close_json_tail
                    if (halt != {"node": halt.get("node"), "kind": halt.get("kind"), "reason": "JSONDecodeError"}
                            or (halt.get("node"), halt.get("kind")) not in stages
                            or len(self.rows) != stages.index((halt["node"], halt["kind"])) + 1
                            or close_json_tail(content) is None):
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "CAPTURED_JSON_TAIL_CLOSED_WITHOUT_TEXT_CHANGE"
                elif halt in ({"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": "UNBOUND_RESEARCH_NUMBER"},
                              {"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": "THESIS_EVIDENCE_CHECK_CHANGED"}):
                    if len(self.rows) != 17:
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "COMPLETE_CAPTURED_FINAL_REVALIDATED"
                    new_primary_calls_allowed = False
                elif halt == {"node": "Research Manager", "kind": "ResearchEvaluation", "reason": "THESIS_CLAIM_COVERAGE_INVALID"}:
                    expected = [row["id"] for row in json.loads(captured["messages"][0][1]["content"])["updated_claims"]]
                    given = [row["claim_id"] for row in original_value["assessments"]]
                    if (len(self.rows) != 9
                            or original_value != types["ResearchEvaluation"].model_validate(original_value).model_dump(mode="json")
                            or (len(given) == len(expected) and set(given) == set(expected))):
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "CAPTURED_INTERMEDIATE_MANAGER_COVERAGE_GAPS_PRESERVED"
                elif halt == {"node": "Portfolio Manager", "kind": "ForwardRevision", "reason": "THESIS_DECISION_UPDATE_INVALID"}:
                    from .thesis_correction import valid_belief_updates
                    if (len(self.rows) != 16 or original_value != types["ForwardRevision"].model_validate(original_value).model_dump(mode="json")
                            or valid_belief_updates(original_value["belief_updates"])
                            or not valid_belief_updates(original_value["belief_updates"], allow_maintain_restatement=True)):
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "CAPTURED_MAINTAIN_RESTATEMENTS_PRESERVED_NOT_CERTIFIED_EQUIVALENT"
                elif halt.get("node") in ANALYSTS and halt == {"node": halt.get("node"), "kind": "AnalystReport", "reason": "ValidationError"}:
                    normalized = normalize_limits(original_value)
                    if (len(self.rows) != list(ANALYSTS).index(halt["node"]) + 1 or normalized == original_value
                            or normalized != types["AnalystReport"].model_validate(normalized).model_dump(mode="json")):
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "CAPTURED_ANALYST_LIMITS_REFLOWED_WITHOUT_TEXT_LOSS"
                elif (halt.get("kind") in NOTE_KINDS and (halt.get("node"), halt.get("kind")) in stages
                        and halt == {"node": halt["node"], "kind": halt["kind"], "reason": "ValidationError"}):
                    halted_kind = halt["kind"]
                    normalized = normalize_role(original_value, halted_kind)
                    if (len(self.rows) != stages.index((halt["node"], halted_kind)) + 1 or normalized == original_value
                            or normalized != types[halted_kind].model_validate(normalized).model_dump(mode="json", exclude_unset=True)):
                        raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                    replay_validation = "CAPTURED_CITATION_NOTE_MERGED_WITHOUT_TEXT_LOSS"
                else:
                    raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
            else:
                expected_halt = {"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": "UNBOUND_RESEARCH_NUMBER"}
                if (not current or self.recovery_state["halted"] != expected_halt
                        or prefix_stop is not None or len(self.rows) != 13 or len(stages) != 13):
                    raise ValueError("THESIS_PRESENTATION_REPLAY_NOT_PROVEN")
                new_primary_calls_allowed = False
                replay_validation = "COMPLETE_CAPTURED_FINAL_REVALIDATED"
            # The captured prefix has passed the current parsers and checks.
            # Its node-level validations run again during reuse. Only the
            # declared remaining stages may make new calls; originals persist.
            self.receipt["presentation_replay"] = {"original_runtime_sha256": sha256_hex(raw),
                "original_halt": deepcopy(expected_halt), "captured_final_run_id": self.rows[-1]["run_id"],
                "validation": replay_validation, "new_primary_calls_allowed": new_primary_calls_allowed}
            self.recovery_state["halted"] = None
        if protocol_version >= 11:
            self.receipt["prior_final_generation"] = data.get("final_generation")
        if protocol_version >= 13:
            self.receipt["prefix_stop"] = prefix_stop
        if current:
            self.receipt["budget_origin"] = f"SAME_V{protocol_version}_FLOW"
            self.receipt["prior_model_run_ids"] = [c["run_id"] for c in [*data["recovery"]["retired_final_calls"], *data["model_calls"]]]

    def _dependencies(self, row):
        chain = []
        while row.get("run_id") in self._recovery_dependencies:
            row = self._recovery_dependencies[row["run_id"]]
            chain.append(row)
        return list(reversed(chain))

    def _carry(self, row, capture):
        if not self.current_runtime:
            capture.model_calls.append(deepcopy(row))
            return
        present = {c["run_id"] for c in capture.model_calls}
        for value in [*self._dependencies(row), row]:
            if value["run_id"] not in present:
                capture.model_calls.append(deepcopy(value))
                present.add(value["run_id"])

    def take(self, node, prompts, capture):
        if self.used == len(self.rows):
            return None
        row = self.rows[self.used]
        actual = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]}
                  for m in row["messages"][0]]
        dependencies = self._dependencies(row)
        if dependencies:
            from .thesis_recovery import call_messages
            matching = call_messages(dependencies[0])
        else:
            matching = actual
        if row["node"] != node or not any(canonical_json_bytes(matching) == canonical_json_bytes(p) for p in prompts):
            raise ValueError("THESIS_RESUME_MESSAGE_MISMATCH")
        from langchain_core.messages import AIMessage
        if len(row["output"]) != 1:
            raise ValueError("THESIS_RESUME_RESPONSE_INVALID")
        value = row["output"][0]
        message = AIMessage(content=value["content"], id=value["id"], tool_calls=value.get("tool_calls", []),
                            usage_metadata=value.get("usage"), response_metadata={"finish_reason": value.get("finish_reason")})
        self._carry(row, capture)
        self.used += 1
        return message, actual

    def carry_halted_calls(self, node, kind, capture):
        if self.recovery_state is None or self.recovery_state["halted"] is None:
            return
        halted = self.recovery_state["halted"]
        if (halted["node"], halted["kind"]) != (node, kind):
            return
        present = {c["run_id"] for c in capture.model_calls}
        for call in self._prior_calls:
            if (call["node"] == node and (call.get("thesis_stage") or {}).get("kind") == kind
                    and call["run_id"] not in present):
                capture.model_calls.append(deepcopy(call))

    def carry_pending_call(self, entry, prompts, capture):
        from .thesis_recovery import call_messages
        found = [c for c in self._prior_calls if c["run_id"] == entry["failed_run_id"]]
        chain = self._dependencies(found[0]) if len(found) == 1 else []
        if len(found) != 1 or call_messages(chain[0] if chain else found[0]) not in prompts:
            raise ValueError("THESIS_PENDING_RECOVERY_INPUT_MISMATCH")
        call = deepcopy(found[0])
        self._carry(call, capture)
        return call

    def complete_checkpoint_calls(self, capture, state):
        """An interruption while replaying a prefix must not orphan its ledger."""
        if not self._prior_calls or state is None:
            return
        retired = {c["run_id"] for c in state["retired_final_calls"]}
        original = {c["run_id"] for c in self._prior_calls}
        present = {c["run_id"] for c in capture.model_calls}
        needed = {identity for r in state["attempts"] for identity in (r["failed_run_id"], r["retry_run_id"])
                  if identity in original and identity not in retired}
        if not needed <= present and present <= original:
            capture.model_calls[:] = [deepcopy(c) for c in self._prior_calls if c["run_id"] not in retired]
