"""One immutable draft Case per investigation run; no duplicate review engine."""

import json

from finauditgate.cashflow import CashflowCaseView
from finauditgate.contracts import RunRef
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.cashflow import read_record
from finauditgate.application.cashflow_report import render
from finauditgate.application.contracts import ApplicationError, _validate_case_ref


def save(application_root, outcome):
    report, evidence = render(outcome.report)
    identity = {"schema_version": "finresearchops.cashflow-case/v1", "run_id": outcome.run_ref.run_id}
    case_ref = "case-" + sha256_hex(canonical_json_bytes(identity))
    directory = application_root / "cashflow-cases" / case_ref
    write_once(directory / "workpaper.html", report)
    write_once(directory / "evidence.html", evidence)
    write_once(directory / "case.json", canonical_json_bytes({**identity, "case_ref": case_ref,
               "workpaper_sha256": sha256_hex(report), "evidence_sha256": sha256_hex(evidence)}))
    return case_ref


def load(application_root, gate, core_root, case_ref):
    _validate_case_ref(case_ref)
    directory = application_root / "cashflow-cases" / case_ref
    try:
        payload = json.loads((directory / "case.json").read_bytes())
        if (set(payload) != {"schema_version", "run_id", "case_ref", "workpaper_sha256", "evidence_sha256"}
                or payload["schema_version"] != "finresearchops.cashflow-case/v1"):
            raise ValueError("CASHFLOW_CASE_SCHEMA_INVALID")
        identity = {"schema_version": "finresearchops.cashflow-case/v1", "run_id": payload["run_id"]}
        if payload["case_ref"] != case_ref or case_ref != "case-" + sha256_hex(canonical_json_bytes(identity)):
            raise ValueError("CASE_IDENTITY_MISMATCH")
        ref = RunRef(payload["run_id"])
        replay = gate.replay(ref)
        if not replay.consistent:
            raise ValueError("CASHFLOW_REPLAY_FAILED")
        record = read_record(core_root, ref)
        report, evidence = render(record)
        for filename, expected, digest in (("workpaper.html", report, payload["workpaper_sha256"]),
                                            ("evidence.html", evidence, payload["evidence_sha256"])):
            if (directory / filename).read_bytes() != expected or sha256_hex(expected) != digest:
                raise ValueError("WORKPAPER_CORE_MISMATCH")
        return CashflowCaseView(case_ref, "AWAITING_REVIEW", (ref,),
                                (str(directory / "workpaper.html"),), record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ApplicationError("CASHFLOW_CASE_INTEGRITY_FAILED") from exc
