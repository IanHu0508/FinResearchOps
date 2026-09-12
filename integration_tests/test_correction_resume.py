"""Do not replay schema-valid responses that failed semantic acceptance.

All receipts and model outputs are synthetic and held in memory. No provider or
filesystem artifact is used to exercise the completed-prefix reader.
"""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from test_thesis_correction import CorrectionLLM, correction_sources

from finauditgate.adapters.thesis_correction import correction_schemas
from finauditgate.adapters.thesis_protocol import schemas
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.core.forward_revision import apply_forward_revision
from finauditgate.core.forward_scenarios import calculate_forward


_STAGES = [
    ("Bull Researcher", "InitialBrief"), ("Bear Researcher", "InitialBrief"),
    ("Bull Researcher", "RevisionBrief"), ("Bear Researcher", "RevisionBrief"),
    ("Research Manager", "ResearchEvaluation"), ("Trader", "ExecutionReview"),
    ("Aggressive Analyst", "RiskBrief"), ("Conservative Analyst", "RiskBrief"),
    ("Neutral Analyst", "RiskBrief"), ("Portfolio Manager", "IndependentAssessment"),
    ("Portfolio Manager", "UnderwritingDraft"), ("Portfolio Manager", "ForwardRevision"),
    ("Portfolio Manager", "FinalResearchReport"),
]


def completed_receipt():
    types = schemas()
    types.update(correction_schemas(types))
    model = CorrectionLLM()
    sources, request = correction_sources(), {"symbol": "AURORA"}
    initial, claims, beliefs, rows = {}, [], [], []
    for index, (node, kind) in enumerate(_STAGES):
        payload = {"node": node, "request": request, "source_bundle": sources}
        if kind == "RevisionBrief":
            other = "Bear Researcher" if node == "Bull Researcher" else "Bull Researcher"
            payload.update(own_initial=initial[node], opponent_initial=initial[other])
        if index >= 4:
            payload["updated_claims"] = claims
        if kind == "ForwardRevision":
            payload.update(forward_draft=draft, forward_calculations=calculate_forward(draft),
                           independent_beliefs=beliefs)
        if kind == "FinalResearchReport":
            payload.update(effective_forward_draft=effective["effective_forward_draft"],
                           effective_forward_calculations=effective["effective_forward_calculations"])
        # Directly call the synthetic response generator, without a model client.
        raw = model._generate([HumanMessage(content=json.dumps(payload))],
                              synthetic_schema=types[kind]).generations[0].message
        value = json.loads(raw.content)
        types[kind].model_validate(value)
        if kind == "InitialBrief":
            prefix = "B" if node == "Bull Researcher" else "S"
            initial[node] = {"claims": [{"id": f"{prefix}{i}", **claim}
                                        for i, claim in enumerate(value["claims"], 1)]}
            claims.extend(initial[node]["claims"])
        if kind == "IndependentAssessment":
            beliefs = value["beliefs"]
        if kind == "UnderwritingDraft":
            draft = value
        if kind == "ForwardRevision":
            effective = apply_forward_revision(draft, value["changes"])
        rows.append({"node": node,
                     "messages": [[{"type": "system", "content": "SYNTHETIC_PROTOCOL"},
                                   {"type": "human", "content": json.dumps(payload)}]],
                     "output": [{"id": f"synthetic-completed-{index + 1}", "content": raw.content}]})
    receipt = {"schema_version": "finresearchops.thesis-runtime/v1", "model_calls": rows,
               "budget": {"calls": 13, "usage": [{"input_tokens": 100, "output_tokens": 20}] * 13},
               "final_generation": [{"status": "COMPLETED", "response_id": rows[-1]["output"][0]["id"]}]}
    return types, request, sources, receipt


class CorrectionResumeTests(unittest.TestCase):
    @staticmethod
    def resume(request, sources, receipt):
        files = {
            "request.json": json.dumps({"request": request, "sources": sources}).encode(),
            "runtime-receipt.json": json.dumps(receipt).encode(),
        }

        def read(path):
            return files[path.name]

        with patch.object(Path, "read_bytes", read), patch.object(Path, "glob", return_value=[]):
            return CompletedCalls("/synthetic-in-memory", request, sources, "deepseek-flash", protocol_version=11)

    def assert_prefix_stops(self, request, sources, receipt, expected):
        original = deepcopy(receipt)
        resumed = self.resume(request, sources, receipt)
        self.assertEqual(expected, len(resumed.rows))
        self.assertEqual(expected, resumed.receipt["available_calls"])
        self.assertEqual(receipt["budget"], resumed.receipt["prior_budget"])
        capture = SimpleNamespace(model_calls=[])
        for row in resumed.rows:
            prompt = [{"role": "user" if msg["type"] == "human" else msg["type"], "content": msg["content"]}
                      for msg in row["messages"][0]]
            self.assertIsNotNone(resumed.take(row["node"], [prompt], capture))
        self.assertIsNone(resumed.take("Portfolio Manager", [[]], capture))
        self.assertEqual(expected, len(capture.model_calls))
        self.assertNotIn(receipt["model_calls"][expected]["output"][0]["id"],
                         [row["output"][0]["id"] for row in capture.model_calls])
        self.assertEqual(original, receipt)

    def test_revision_with_invalid_new_statement_stops_before_revision(self):
        types, request, sources, valid = completed_receipt()
        self.assertEqual(13, len(self.resume(request, sources, valid).rows))
        for status, statement in (("revise", None), ("maintain", "Must not be a replacement")):
            with self.subTest(status=status):
                receipt = deepcopy(valid)
                row = receipt["model_calls"][11]
                value = json.loads(row["output"][0]["content"])
                value["belief_updates"][0].update(status=status, new_statement=statement)
                # The missing boundary is semantic, not JSON or Pydantic shape.
                types["ForwardRevision"].model_validate(value)
                row["output"][0]["content"] = json.dumps(value)
                receipt["model_calls"] = receipt["model_calls"][:12]
                receipt["budget"] = {"calls": 12, "usage": receipt["budget"]["usage"][:12]}
                receipt["final_generation"] = []
                self.assert_prefix_stops(request, sources, receipt, 11)

    def test_final_with_incomplete_or_duplicate_scenario_coverage_stops_before_final(self):
        types, request, sources, valid = completed_receipt()
        self.assertEqual(13, len(self.resume(request, sources, valid).rows))
        original = json.loads(valid["model_calls"][12]["output"][0]["content"])
        for assessments in ([], [original["scenario_assessments"][0]] * 2):
            with self.subTest(assessments=assessments):
                receipt = deepcopy(valid)
                value = deepcopy(original)
                value["scenario_assessments"] = assessments
                types["FinalResearchReport"].model_validate(value)
                receipt["model_calls"][12]["output"][0]["content"] = json.dumps(value)
                self.assert_prefix_stops(request, sources, receipt, 12)


if __name__ == "__main__":
    unittest.main()
