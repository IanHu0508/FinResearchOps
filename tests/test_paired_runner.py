from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from finauditgate import AuditTask, FrozenDocumentPackage
from finauditgate.evaluation.paired import (
    PairedArmResult,
    PairedRunner,
)
from finauditgate.private_storage import resolve_private_workspace_anchor


class _Arm:
    def __init__(self, result: PairedArmResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def run(self, task: AuditTask) -> PairedArmResult:
        self.calls.append(task.task_id)
        return self._result


def _task() -> AuditTask:
    return AuditTask(
        task_id="synthetic-paired-item-01",
        question="What was the synthetic growth rate?",
        cutoff=date(2026, 3, 1),
        document=FrozenDocumentPackage(
            source_id="synthetic-paired-source-v1",
            document_name="synthetic-paired.txt",
            document_bytes=b"prior=100\ncurrent=120\n",
            declared_published_at=date(2026, 2, 15),
        ),
    )


def _result(arm_id: str, answer: str) -> PairedArmResult:
    output_bytes = json.dumps(
        {
            "arm_id": arm_id,
            "answer": answer,
            "decision": "ANSWERED",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PairedArmResult(
        arm_id=arm_id,
        output_bytes=output_bytes,
        config_sha256=("a" if arm_id == "baseline" else "b") * 64,
        answer=answer,
        decision="ANSWERED",
        reason_codes=(),
    )


class PairedRunnerTest(unittest.TestCase):
    def test_execution_stays_unscored_until_append_only_human_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            root = private / "paired-evaluation"
            baseline = _Arm(_result("baseline", "19.00"))
            gated = _Arm(_result("gated", "20.00"))
            runner = PairedRunner(
                private_root=root,
                baseline=baseline,
                gated=gated,
            )

            pair_ref = runner.run(_task())
            pair_path = root / pair_ref.relative_path
            pair_payload = json.loads(pair_path.read_bytes())
            qa_ref = runner.record_human_qa(
                pair_ref,
                baseline_correct=False,
                gated_correct=True,
                reviewer="synthetic-test-reviewer",
                rationale="Synthetic oracle says 20.00.",
            )
            qa_path = root / qa_ref.relative_path
            qa_payload = json.loads(qa_path.read_bytes())
            pair_text = pair_path.read_text(encoding="utf-8")
            output_evidence = {
                arm_id: (
                    (root / pair_payload[arm_id]["output_ref"]).is_file(),
                    hashlib.sha256(
                        (root / pair_payload[arm_id]["output_ref"]).read_bytes()
                    ).hexdigest(),
                )
                for arm_id in ("baseline", "gated")
            }

        self.assertEqual([_task().task_id], baseline.calls)
        self.assertEqual([_task().task_id], gated.calls)
        self.assertEqual("AWAITING_HUMAN_QA", pair_payload["qa_status"])
        self.assertNotIn("classification", pair_payload)
        self.assertEqual("WRONG_TO_RIGHT", qa_payload["classification"])
        self.assertEqual(pair_ref.pair_id, qa_payload["pair_id"])
        self.assertNotIn("raw_request", pair_text)
        self.assertNotIn("raw_response", pair_text)
        for arm_id in ("baseline", "gated"):
            arm = pair_payload[arm_id]
            self.assertTrue(output_evidence[arm_id][0])
            self.assertEqual(arm["output_sha256"], output_evidence[arm_id][1])

    def test_right_to_wrong_and_later_qa_are_preserved_not_overwritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            root = private / "paired-evaluation"
            runner = PairedRunner(
                private_root=root,
                baseline=_Arm(_result("baseline", "20.00")),
                gated=_Arm(_result("gated", "19.00")),
            )
            pair_ref = runner.run(_task())
            first = runner.record_human_qa(
                pair_ref,
                baseline_correct=True,
                gated_correct=False,
                reviewer="synthetic-reviewer-a",
                rationale="First independent synthetic check.",
            )
            second = runner.record_human_qa(
                pair_ref,
                baseline_correct=False,
                gated_correct=True,
                reviewer="synthetic-reviewer-b",
                rationale="Second synthetic adjudication is retained.",
            )
            first_payload = json.loads(
                (root / first.relative_path).read_bytes()
            )
            second_payload = json.loads(
                (root / second.relative_path).read_bytes()
            )

        self.assertNotEqual(first.qa_id, second.qa_id)
        self.assertEqual("RIGHT_TO_WRONG", first_payload["classification"])
        self.assertEqual("WRONG_TO_RIGHT", second_payload["classification"])

    def test_private_root_and_pair_integrity_fail_closed(self) -> None:
        repo_root = Path(__file__).parents[1]
        with self.assertRaisesRegex(ValueError, "PRIVATE_STORAGE_REQUIRED"):
            PairedRunner(
                private_root=repo_root / "paired-evaluation",
                baseline=_Arm(_result("baseline", "20.00")),
                gated=_Arm(_result("gated", "20.00")),
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            root = private / "paired-evaluation"
            runner = PairedRunner(
                private_root=root,
                baseline=_Arm(_result("baseline", "20.00")),
                gated=_Arm(_result("gated", "20.00")),
            )
            pair_ref = runner.run(_task())
            (root / pair_ref.relative_path).write_bytes(b"{}")
            with self.assertRaisesRegex(
                RuntimeError,
                "PAIRED_EXECUTION_INTEGRITY_FAILURE",
            ):
                runner.record_human_qa(
                    pair_ref,
                    baseline_correct=True,
                    gated_correct=True,
                    reviewer="synthetic-reviewer",
                    rationale="Must not score a tampered pair.",
                )

    def test_paired_outputs_cannot_cross_an_explicit_workspace_anchor(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory_a,
            tempfile.TemporaryDirectory() as temporary_directory_b,
        ):
            workspace_a = Path(temporary_directory_a)
            workspace_b = Path(temporary_directory_b)
            for workspace in (workspace_a, workspace_b):
                (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
                (workspace / "private").mkdir()
            anchor_a = resolve_private_workspace_anchor(
                workspace_a / "private" / "artifacts",
                purpose="artifact-root",
            )

            with self.assertRaisesRegex(
                ValueError,
                "WORKSPACE_ANCHOR_MISMATCH",
            ):
                PairedRunner(
                    private_root=workspace_b / "private" / "paired-evaluation",
                    baseline=_Arm(_result("baseline", "20.00")),
                    gated=_Arm(_result("gated", "20.00")),
                    private_workspace_anchor=anchor_a,
                )

    def test_unverified_arm_outputs_cannot_receive_human_qa(self) -> None:
        for attack in (
            "nonexistent",
            "tamper",
            "symlink",
            "arm-swap",
        ):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary_directory:
                workspace = Path(temporary_directory)
                (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
                private = workspace / "private"
                private.mkdir()
                root = private / "paired-evaluation"
                runner = PairedRunner(
                    private_root=root,
                    baseline=_Arm(_result("baseline", "19.00")),
                    gated=_Arm(_result("gated", "20.00")),
                )
                pair_ref = runner.run(_task())
                pair = json.loads((root / pair_ref.relative_path).read_bytes())
                baseline_path = root / pair["baseline"]["output_ref"]
                gated_path = root / pair["gated"]["output_ref"]
                baseline_bytes = baseline_path.read_bytes()
                gated_bytes = gated_path.read_bytes()
                if attack == "nonexistent":
                    baseline_path.unlink()
                elif attack == "tamper":
                    baseline_path.write_bytes(b"tampered")
                elif attack == "symlink":
                    baseline_path.unlink()
                    baseline_path.symlink_to(gated_path)
                else:
                    baseline_path.write_bytes(gated_bytes)
                    gated_path.write_bytes(baseline_bytes)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "PAIRED_EXECUTION_INTEGRITY_FAILURE",
                ):
                    runner.record_human_qa(
                        pair_ref,
                        baseline_correct=False,
                        gated_correct=True,
                        reviewer="synthetic-reviewer",
                        rationale="Never score an unverified pair.",
                    )
                qa_root = root / "paired" / "human-qa"
                self.assertFalse(qa_root.exists())


if __name__ == "__main__":
    unittest.main()
