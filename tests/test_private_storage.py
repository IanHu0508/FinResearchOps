from datetime import date
from pathlib import Path
import tempfile
import unittest

from finauditgate import AuditTask, FrozenDocumentPackage
from finauditgate.application import ApplicationError, CreateCase, FinResearchOps
from finauditgate.core.engine import FinAuditGate
from finauditgate.private_storage import (
    PrivateStorageError,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


def _document() -> FrozenDocumentPackage:
    return FrozenDocumentPackage(
        source_id="private-contract-source-v1",
        document_name="private-contract.txt",
        document_bytes=b"private contract fixture\n",
        declared_published_at=date(2026, 8, 13),
    )


def _create_private_case() -> CreateCase:
    return CreateCase(
        question="What changed?",
        cutoff=date(2026, 8, 14),
        document=_document(),
        mode="PRIVATE_DEV",
    )


class PrivateStorageRuntimeTest(unittest.TestCase):
    def test_application_rejects_private_artifacts_inside_the_git_worktree(
        self,
    ) -> None:
        public_repo = Path(__file__).parents[1]

        with self.assertRaises(ApplicationError) as raised:
            FinResearchOps(artifact_root=public_repo / "runtime").handle(
                _create_private_case()
            )

        self.assertEqual("PRIVATE_STORAGE_REQUIRED", raised.exception.code)

    def test_application_rejects_relative_and_symlink_escape_artifact_roots(
        self,
    ) -> None:
        with self.assertRaises(ApplicationError) as relative:
            FinResearchOps(artifact_root=Path("relative-runtime")).handle(
                _create_private_case()
            )
        self.assertEqual("PRIVATE_STORAGE_REQUIRED", relative.exception.code)

        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            outside = workspace / "outside"
            outside.mkdir()
            (private / "escaped").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ApplicationError) as escaped:
                FinResearchOps(
                    artifact_root=private / "escaped" / "runtime"
                ).handle(_create_private_case())
            self.assertEqual(
                "PRIVATE_STORAGE_REQUIRED",
                escaped.exception.code,
            )

    def test_core_checks_private_artifact_root_before_calling_the_model(
        self,
    ) -> None:
        calls: list[str] = []

        class ShouldNotRun:
            def propose(self, task: AuditTask, attempt_index: int = 0) -> object:
                calls.append(task.task_id)
                raise AssertionError("private path check must precede the model")

        task = AuditTask(
            task_id="private-path-preflight",
            question="What changed?",
            cutoff=date(2026, 8, 14),
            document=_document(),
            mode="PRIVATE_DEV",
        )
        public_repo = Path(__file__).parents[1]

        with self.assertRaisesRegex(
            PrivateStorageError,
            "PRIVATE_STORAGE_REQUIRED",
        ):
            FinAuditGate(
                artifact_root=public_repo / "runtime",
                model=ShouldNotRun(),
            ).run(task)

        self.assertEqual([], calls)

    def test_traced_core_rejects_a_public_artifact_root_at_construction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            with self.assertRaisesRegex(
                PrivateStorageError,
                "PRIVATE_STORAGE_REQUIRED:artifact-root",
            ):
                FinAuditGate(
                    artifact_root=workspace / "finaudit-gate" / "runtime",
                    model_trace_root=private / "model-traces",
                )

    def test_valid_workspace_private_root_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            outcome = FinResearchOps(
                artifact_root=private / "runtime"
            ).handle(_create_private_case())

        self.assertTrue(outcome.case_ref.startswith("case-"))

    def test_application_context_rejects_cross_workspace_runtime_roots(
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
            private_a = workspace_a / "private"
            private_b = workspace_b / "private"
            anchor_a = resolve_private_workspace_anchor(
                private_a / "artifacts",
                purpose="artifact-root",
            )

            self.assertEqual(
                (private_a / "model-traces").resolve(),
                require_private_storage_root(
                    private_a / "model-traces",
                    purpose="model-trace-root",
                    anchor=anchor_a,
                ),
            )
            with self.assertRaisesRegex(
                PrivateStorageError,
                "WORKSPACE_ANCHOR_MISMATCH",
            ):
                FinResearchOps(
                    artifact_root=private_a / "artifacts",
                    model_trace_root=private_b / "model-traces",
                )


if __name__ == "__main__":
    unittest.main()
