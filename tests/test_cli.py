from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from finauditgate import Decision, RunRef
from finauditgate.application import (
    ApplicationOperation,
    ApplicationOutcome,
    ApplicationError,
    CaseView,
    CaseStatus,
    CreateCase,
    ExportChangePacket,
    ReplayRun,
    ReviewAction,
    RunAnalysis,
    SourceRefView,
    SubmitReview,
)
import finauditgate.cli as cli


def _private_root(temporary_directory: str) -> Path:
    workspace = Path(temporary_directory)
    (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
    private = workspace / "private"
    private.mkdir()
    return private


class FinResearchOpsCLITest(unittest.TestCase):
    def test_application_failure_is_a_stable_json_cli_error(self) -> None:
        case_ref = f"case-{'a' * 64}"

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                pass

            def read_case(self, received_case_ref: str) -> CaseView:
                raise ApplicationError("CASE_NOT_FOUND")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "inspect-case",
                        "--case-ref",
                        case_ref,
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            {
                "error": {
                    "code": "CASE_NOT_FOUND",
                    "family": "APPLICATION_ERROR",
                },
                "schema_version": "finresearchops.cli-error/v1",
            },
            json.loads(stderr.getvalue()),
        )

    def test_package_exposes_the_finresearchops_console_entrypoint(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            "finauditgate.cli:main",
            pyproject["project"]["scripts"]["finresearchops"],
        )

    def test_replay_dispatches_only_the_frozen_run_reference(self) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"
        run_ref = RunRef("b" * 64)
        replay_ref = "c" * 64

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["constructor"] = kwargs

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.REPLAY_RUN,
                    case_ref=case_ref,
                    status=CaseStatus.AWAITING_REVIEW,
                    run_ref=run_ref,
                    replay_ref=replay_ref,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "replay",
                        "--run-id",
                        run_ref.run_id,
                        "--model-trace-root",
                        str(root / "private-traces"),
                    ]
                )

        command = captured["command"]
        self.assertIs(type(command), ReplayRun)
        self.assertEqual(run_ref, command.run_ref)
        self.assertEqual(
            root / "private-traces",
            captured["constructor"]["model_trace_root"],
        )
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "operation": "REPLAY_RUN",
                "reason_codes": [],
                "replay_ref": replay_ref,
                "run_ref": {"run_id": run_ref.run_id},
                "status": "AWAITING_REVIEW",
            },
            json.loads(stdout.getvalue()),
        )

    def test_export_requests_a_packet_without_caller_supplied_research_state(
        self,
    ) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"
        run_ref = RunRef("b" * 64)
        packet_ref = "c" * 64

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["constructor"] = kwargs

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.EXPORT_CHANGE_PACKET,
                    case_ref=case_ref,
                    status=CaseStatus.APPROVED,
                    run_ref=run_ref,
                    packet_ref=packet_ref,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "export",
                        "--case-ref",
                        case_ref,
                        "--run-id",
                        run_ref.run_id,
                    ]
                )

        command = captured["command"]
        self.assertIs(type(command), ExportChangePacket)
        self.assertEqual(case_ref, command.case_ref)
        self.assertEqual(run_ref, command.run_ref)
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "operation": "EXPORT_CHANGE_PACKET",
                "packet_ref": packet_ref,
                "reason_codes": [],
                "run_ref": {"run_id": run_ref.run_id},
                "status": "APPROVED",
            },
            json.loads(stdout.getvalue()),
        )

    def test_review_dispatches_the_closed_human_action(self) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"
        run_ref = RunRef("b" * 64)
        review_ref = "c" * 64

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["constructor"] = kwargs

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.SUBMIT_REVIEW,
                    case_ref=case_ref,
                    status=CaseStatus.RETURNED,
                    run_ref=run_ref,
                    review_ref=review_ref,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "review",
                        "--case-ref",
                        case_ref,
                        "--run-id",
                        run_ref.run_id,
                        "--action",
                        "RETURN",
                        "--reason",
                        "Fiscal period needs analyst confirmation.",
                    ]
                )

        command = captured["command"]
        self.assertIs(type(command), SubmitReview)
        self.assertEqual(case_ref, command.case_ref)
        self.assertEqual(run_ref, command.run_ref)
        self.assertIs(ReviewAction.RETURN, command.action)
        self.assertEqual(
            "Fiscal period needs analyst confirmation.",
            command.reason,
        )
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "operation": "SUBMIT_REVIEW",
                "reason_codes": [],
                "review_ref": review_ref,
                "run_ref": {"run_id": run_ref.run_id},
                "status": "RETURNED",
            },
            json.loads(stdout.getvalue()),
        )

    def test_inspect_case_reads_the_application_view(self) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"
        view = CaseView(
            case_ref=case_ref,
            question="What changed?",
            cutoff=date(2026, 8, 14),
            source_ref=SourceRefView(
                source_id="private-user-frozen-source-v1",
                document_name="frozen.txt",
                document_sha256="b" * 64,
                declared_published_at=date(2026, 8, 13),
            ),
            status=CaseStatus.CREATED,
            run_refs=(),
            workpapers=(),
            reviews=(),
            packets=(),
            replays=(),
            latest_machine_decision=None,
            latest_gate_reason_codes=(),
        )

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["constructor"] = kwargs

            def read_case(self, received_case_ref: str) -> CaseView:
                captured["case_ref"] = received_case_ref
                return view

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "inspect-case",
                        "--case-ref",
                        case_ref,
                    ]
                )

        self.assertEqual(case_ref, captured["case_ref"])
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "cutoff": "2026-08-14",
                "latest_gate_reason_codes": [],
                "latest_machine_decision": None,
                "packets": [],
                "question": "What changed?",
                "replays": [],
                "reviews": [],
                "run_refs": [],
                "source_ref": {
                    "declared_published_at": "2026-08-13",
                    "document_name": "frozen.txt",
                    "document_sha256": "b" * 64,
                    "source_id": "private-user-frozen-source-v1",
                },
                "status": "CREATED",
                "workpapers": [],
            },
            json.loads(stdout.getvalue()),
        )

    def test_run_analysis_wires_the_local_adapter_and_private_trace_root(self) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"
        run_ref = RunRef("b" * 64)
        workpaper_ref = "c" * 64
        profile = object()

        class FakeModel:
            def __init__(self, **kwargs: object) -> None:
                captured["model_constructor"] = kwargs

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["application_constructor"] = kwargs

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.RUN_ANALYSIS,
                    case_ref=case_ref,
                    status=CaseStatus.AWAITING_REVIEW,
                    run_ref=run_ref,
                    workpaper_ref=workpaper_ref,
                    machine_decision=Decision.HUMAN_REVIEW,
                    reason_codes=("FISCAL_PERIOD_CONFLICT",),
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.OllamaModelAdapter", FakeModel),
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                patch(
                    "finauditgate.cli.PrivateDevValidationProfile.from_path",
                    return_value=profile,
                ) as load_profile,
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "run-analysis",
                        "--case-ref",
                        case_ref,
                        "--model-trace-root",
                        str(root / "private-traces"),
                        "--validation-profile",
                        str(root / "private-profile.json"),
                    ]
                )

        self.assertIs(type(captured["command"]), RunAnalysis)
        self.assertEqual(case_ref, captured["command"].case_ref)
        workspace_anchor = captured["model_constructor"][
            "private_workspace_anchor"
        ]
        self.assertEqual(
            root / "private-traces",
            captured["model_constructor"]["trace_root"],
        )
        self.assertEqual(root.resolve(), workspace_anchor.private_root)
        self.assertIs(
            captured["application_constructor"]["model"].__class__,
            FakeModel,
        )
        self.assertEqual(
            root / "private-traces",
            captured["application_constructor"]["model_trace_root"],
        )
        self.assertIs(
            workspace_anchor,
            captured["application_constructor"]["private_workspace_anchor"],
        )
        self.assertIs(
            profile,
            captured["application_constructor"]["private_dev_profile"],
        )
        load_profile.assert_called_once_with(
            root / "private-profile.json",
            private_workspace_anchor=workspace_anchor,
        )
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "machine_decision": "HUMAN_REVIEW",
                "operation": "RUN_ANALYSIS",
                "reason_codes": ["FISCAL_PERIOD_CONFLICT"],
                "run_ref": {"run_id": run_ref.run_id},
                "status": "AWAITING_REVIEW",
                "workpaper_ref": workpaper_ref,
            },
            json.loads(stdout.getvalue()),
        )

    def test_public_model_trace_path_fails_as_stable_non_leaking_error(
        self,
    ) -> None:
        repo_root = Path(__file__).parents[1]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(
                [
                    "--artifact-root",
                    str(repo_root / "runtime"),
                    "run-analysis",
                    "--case-ref",
                    f"case-{'a' * 64}",
                    "--model-trace-root",
                    str(repo_root / "raw-traces"),
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        error_text = stderr.getvalue()
        self.assertNotIn(str(repo_root), error_text)
        self.assertEqual(
            {
                "error": {
                    "code": "PRIVATE_STORAGE_REQUIRED",
                    "family": "APPLICATION_ERROR",
                },
                "schema_version": "finresearchops.cli-error/v1",
            },
            json.loads(error_text),
        )

    def test_public_artifact_root_fails_before_any_document_read(self) -> None:
        repo_root = Path(__file__).parents[1]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(
                [
                    "--artifact-root",
                    str(repo_root / "runtime"),
                    "create-case",
                    "--question",
                    "Synthetic path preflight?",
                    "--cutoff",
                    "2026-08-14",
                    "--document",
                    str(repo_root / "must-not-be-read.txt"),
                    "--source-id",
                    "synthetic-path-preflight",
                    "--published-at",
                    "2026-08-13",
                    "--mode",
                    "SYNTHETIC_DEV",
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn(str(repo_root), stderr.getvalue())
        self.assertEqual(
            "PRIVATE_STORAGE_REQUIRED",
            json.loads(stderr.getvalue())["error"]["code"],
        )

    def test_private_dev_document_must_cross_the_private_input_seam(self) -> None:
        repo_root = Path(__file__).parents[1]
        attacks: dict[str, Path] = {
            "repo": repo_root / "README.md",
            "relative": Path("README.md"),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_root(temporary_directory)
            (private / "repo-link").symlink_to(repo_root / "README.md")
            attacks["symlink"] = private / "repo-link"
            for name, document_path in attacks.items():
                with self.subTest(name=name):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = cli.main(
                            [
                                "--artifact-root",
                                str(private / "artifacts"),
                                "create-case",
                                "--question",
                                "Private input seam?",
                                "--cutoff",
                                "2026-08-14",
                                "--document",
                                str(document_path),
                                "--source-id",
                                "private-input-attack",
                                "--published-at",
                                "2026-08-13",
                                "--mode",
                                "PRIVATE_DEV",
                            ]
                        )

                    self.assertEqual(2, exit_code)
                    self.assertEqual("", stdout.getvalue())
                    self.assertEqual(
                        "PRIVATE_STORAGE_REQUIRED",
                        json.loads(stderr.getvalue())["error"]["code"],
                    )
                    self.assertNotIn(str(repo_root), stderr.getvalue())
                    self.assertNotIn(str(private), stderr.getvalue())

    def test_private_document_read_failure_is_stable_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_root(temporary_directory)
            document_path = private / "unreadable.txt"
            document_path.write_bytes(b"private bytes")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=OSError(str(document_path)),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(private / "artifacts"),
                        "create-case",
                        "--question",
                        "Private read failure?",
                        "--cutoff",
                        "2026-08-14",
                        "--document",
                        str(document_path),
                        "--source-id",
                        "private-read-failure",
                        "--published-at",
                        "2026-08-13",
                        "--mode",
                        "PRIVATE_DEV",
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn(str(document_path), stderr.getvalue())
        self.assertEqual(
            "PRIVATE_STORAGE_REQUIRED",
            json.loads(stderr.getvalue())["error"]["code"],
        )

    def test_private_dev_rejects_mixed_workspace_roots(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory_a,
            tempfile.TemporaryDirectory() as temporary_directory_b,
        ):
            private_a = _private_root(temporary_directory_a)
            private_b = _private_root(temporary_directory_b)
            document_path = private_b / "issuer-disclosure.txt"
            document_path.write_bytes(b"private issuer disclosure\n")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(private_a / "artifacts"),
                        "create-case",
                        "--question",
                        "What changed?",
                        "--cutoff",
                        "2026-08-14",
                        "--document",
                        str(document_path),
                        "--source-id",
                        "mixed-workspace-attack",
                        "--published-at",
                        "2026-08-13",
                        "--mode",
                        "PRIVATE_DEV",
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            "PRIVATE_STORAGE_REQUIRED",
            json.loads(stderr.getvalue())["error"]["code"],
        )
        self.assertNotIn(str(private_a), stderr.getvalue())
        self.assertNotIn(str(private_b), stderr.getvalue())

    def test_trace_aware_actions_reject_mixed_workspace_roots(self) -> None:
        case_ref = f"case-{'a' * 64}"
        run_id = "b" * 64
        actions = (
            ("run-analysis", "--case-ref", case_ref),
            ("inspect-case", "--case-ref", case_ref),
            (
                "review",
                "--case-ref",
                case_ref,
                "--run-id",
                run_id,
                "--action",
                "RETURN",
                "--reason",
                "Cross-root attack regression.",
            ),
            ("export", "--case-ref", case_ref, "--run-id", run_id),
            ("replay", "--run-id", run_id),
        )
        with (
            tempfile.TemporaryDirectory() as temporary_directory_a,
            tempfile.TemporaryDirectory() as temporary_directory_b,
        ):
            private_a = _private_root(temporary_directory_a)
            private_b = _private_root(temporary_directory_b)
            for action in actions:
                with self.subTest(action=action[0]):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = cli.main(
                            [
                                "--artifact-root",
                                str(private_a / "artifacts"),
                                *action,
                                "--model-trace-root",
                                str(private_b / "model-traces"),
                            ]
                        )

                    self.assertEqual(2, exit_code)
                    self.assertEqual("", stdout.getvalue())
                    self.assertEqual(
                        "PRIVATE_STORAGE_REQUIRED",
                        json.loads(stderr.getvalue())["error"]["code"],
                    )
                    self.assertNotIn(str(private_a), stderr.getvalue())
                    self.assertNotIn(str(private_b), stderr.getvalue())

    def test_validation_profile_rejects_a_mixed_workspace_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory_a,
            tempfile.TemporaryDirectory() as temporary_directory_b,
        ):
            private_a = _private_root(temporary_directory_a)
            private_b = _private_root(temporary_directory_b)
            profile_path = private_b / "profile.json"
            profile_path.write_bytes(b"{}")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(private_a / "artifacts"),
                        "run-analysis",
                        "--case-ref",
                        f"case-{'a' * 64}",
                        "--model-trace-root",
                        str(private_a / "model-traces"),
                        "--validation-profile",
                        str(profile_path),
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            "PRIVATE_STORAGE_REQUIRED",
            json.loads(stderr.getvalue())["error"]["code"],
        )
        self.assertNotIn(str(private_a), stderr.getvalue())
        self.assertNotIn(str(private_b), stderr.getvalue())

    def test_synthetic_dev_may_read_a_public_synthetic_fixture(self) -> None:
        captured: dict[str, object] = {}
        fixture = (
            Path(__file__).parents[1]
            / "fixtures"
            / "synthetic"
            / "aurora_revenue_growth_m2.txt"
        )

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                pass

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.CREATE_CASE,
                    case_ref=f"case-{'d' * 64}",
                    status=CaseStatus.CREATED,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_root(temporary_directory)
            with patch("finauditgate.cli.FinResearchOps", FakeApplication):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(private / "artifacts"),
                        "create-case",
                        "--question",
                        "Synthetic fixture?",
                        "--cutoff",
                        "2026-08-14",
                        "--document",
                        str(fixture),
                        "--source-id",
                        "synthetic-public-fixture",
                        "--published-at",
                        "2026-08-13",
                        "--mode",
                        "SYNTHETIC_DEV",
                    ]
                )

        self.assertEqual(0, exit_code)
        command = captured["command"]
        self.assertIs(type(command), CreateCase)
        self.assertEqual(fixture.read_bytes(), command.document.document_bytes)

    def test_create_case_is_a_thin_application_command_adapter(self) -> None:
        captured: dict[str, object] = {}
        case_ref = f"case-{'a' * 64}"

        class FakeApplication:
            def __init__(self, **kwargs: object) -> None:
                captured["constructor"] = kwargs

            def handle(self, command: object) -> ApplicationOutcome:
                captured["command"] = command
                return ApplicationOutcome(
                    operation=ApplicationOperation.CREATE_CASE,
                    case_ref=case_ref,
                    status=CaseStatus.CREATED,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _private_root(temporary_directory)
            document_path = root / "frozen.txt"
            document_path.write_bytes(b"frozen issuer bytes\n")
            stdout = io.StringIO()
            with (
                patch("finauditgate.cli.FinResearchOps", FakeApplication),
                redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--artifact-root",
                        str(root / "artifacts"),
                        "create-case",
                        "--question",
                        "What changed?",
                        "--cutoff",
                        "2026-08-14",
                        "--document",
                        str(document_path),
                        "--source-id",
                        "private-user-frozen-source-v1",
                        "--published-at",
                        "2026-08-13",
                        "--mode",
                        "PRIVATE_DEV",
                    ]
                )

        command = captured["command"]
        self.assertIs(type(command), CreateCase)
        self.assertEqual("What changed?", command.question)
        self.assertEqual(date(2026, 8, 14), command.cutoff)
        self.assertEqual(b"frozen issuer bytes\n", command.document.document_bytes)
        self.assertEqual("private-user-frozen-source-v1", command.document.source_id)
        self.assertEqual("frozen.txt", command.document.document_name)
        self.assertEqual(date(2026, 8, 13), command.document.declared_published_at)
        self.assertEqual("PRIVATE_DEV", command.mode)
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "case_ref": case_ref,
                "operation": "CREATE_CASE",
                "reason_codes": [],
                "status": "CREATED",
            },
            json.loads(stdout.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
