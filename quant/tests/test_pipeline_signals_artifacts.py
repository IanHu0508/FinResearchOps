from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quant.artifacts import read_run, write_run
from quant.contracts import ContractError, Prediction, primitive
from quant.data.serialization import from_document, to_document
from quant.inference import build_signals, validate_signal
from quant.inference.signals import FIELDS
from quant.models.baseline import MeanModel, NearestNeighborsModel, restore_reference_model
from quant.pipeline import prepare_dataset, run_experiment
from quant.splits import prepare_fold
from quant.synthetic import make_synthetic_data, synthetic_fold

ROOT = Path(__file__).resolve().parents[2]


class PipelineSignalArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.spec = make_synthetic_data()
        cls.dataset = prepare_dataset(cls.data, cls.spec)
        cls.window = synthetic_fold(cls.data)
        cls.fold = prepare_fold(cls.dataset, cls.window)
        cls.model = MeanModel().fit(cls.fold.train)
        cls.as_ofs = tuple(sorted({r.key.as_of for r in cls.fold.test.rows}))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        (self.workspace / "finaudit-gate").mkdir()
        (self.workspace / "finaudit-gate/pyproject.toml").write_text("# synthetic workspace")
        self.output = self.workspace / "private/run"

    def signals(self, predictions=None, panel=None):
        return build_signals(predictions or self.model.predict(self.fold.test.rows), panel or self.dataset.panel,
            as_ofs=self.as_ofs, model=self.model, dataset_id=self.dataset.dataset_id)

    def test_predicted_target_and_current_model_rank_are_distinct(self):
        keys = tuple(r.key for r in self.fold.test.rows)
        values = {symbol: 0.70 + i * 0.02 for i, symbol in enumerate(self.data.universes[0].symbols)}
        predictions = tuple(Prediction(k, values[k.symbol]) for k in keys)
        result = self.signals(predictions)
        first_day = result[:6]
        self.assertAlmostEqual(0.8, first_day[-1]["predicted_target_percentile"])
        self.assertEqual(1.0, first_day[-1]["cross_sectional_model_rank"])
        self.assertEqual("RESEARCH_ONLY_NOT_TRADE_OR_PROBABILITY", first_day[-1]["usage"])

    def test_missing_stock_cannot_be_presented_as_complete_universe_rank(self):
        predictions = self.model.predict(self.fold.test.rows)
        with self.assertRaisesRegex(ContractError, "COVERAGE"):
            self.signals(predictions[1:])

    def test_missing_entire_requested_day_is_rejected(self):
        predictions = tuple(p for p in self.model.predict(self.fold.test.rows) if p.key.as_of != self.as_ofs[0])
        with self.assertRaisesRegex(ContractError, "COVERAGE"):
            self.signals(predictions)

    def test_signal_rejects_future_training_and_future_data(self):
        signal = self.signals()[0]
        for key in ("training_cutoff", "data_cutoff"):
            future = (self.fold.test.rows[0].key.as_of + timedelta(days=1)).isoformat()
            with self.subTest(key=key), self.assertRaisesRegex(ContractError, "FUTURE_INFORMATION"):
                validate_signal({**signal, key: future})

    def test_signal_rejects_action_probability_unknown_field_and_bad_units(self):
        signal = self.signals()[0]
        for updates in ({"action": "BUY"}, {"probability_up": 0.9}, {"horizon_unit": "calendar_days"},
                        {"horizon": 5}, {"predicted_target_percentile": float("nan")},
                        {"cross_sectional_model_rank": True}, {"dataset_id": "not-a-dataset"},
                        {"feature_ablation": "unknown"}, {"cross_sectional_rank": 0.5}):
            with self.subTest(updates=updates), self.assertRaises((ContractError, ValueError)):
                validate_signal({**signal, **updates})

    def test_signal_schema_matches_public_validator_fields_and_constants(self):
        schema = json.loads((ROOT / "schemas/quant-signal.v3.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(FIELDS, set(schema["required"]))
        self.assertEqual(FIELDS, set(schema["properties"]))
        for signal in self.signals()[:6]:
            for name, rule in schema["properties"].items():
                if "const" in rule:
                    self.assertEqual(rule["const"], signal[name])
                if "minimum" in rule:
                    self.assertGreaterEqual(signal[name], rule["minimum"])
                if "maximum" in rule:
                    self.assertLessEqual(signal[name], rule["maximum"])
            self.assertEqual(signal, validate_signal(signal))

    def test_market_only_input_json_roundtrip_preserves_dataset_identity(self):
        recovered = from_document(json.loads(json.dumps(to_document(self.data))))
        self.assertEqual(self.data, recovered)
        self.assertEqual(self.dataset.dataset_id, prepare_dataset(recovered, self.spec).dataset_id)

    def test_input_rejects_unversioned_and_unknown_fields(self):
        for extra in ({"schema_version": "unknown"}, {"schema_version": "quant.research-input/v1"},
                      {"unexpected": 2}, {"industries": []}, {"valuations": []}, {"disclosures": []}):
            with self.assertRaises(ContractError):
                from_document({**to_document(self.data), **extra})

    def test_ablation_changes_model_and_signal_metadata_not_dataset_or_splits(self):
        stock = run_experiment(self.data, self.spec, self.window, MeanModel(ablation="stock-only"))
        context = run_experiment(self.data, self.spec, self.window, MeanModel(ablation="stock+context"))
        self.assertEqual(stock["dataset_id"], context["dataset_id"])
        self.assertEqual(stock["counts"], context["counts"])
        self.assertEqual(stock["fold"], context["fold"])
        self.assertNotEqual(stock["model_version"], context["model_version"])
        self.assertEqual("stock-only", stock["signals"][0]["feature_ablation"])
        self.assertEqual("stock+context", context["signals"][0]["feature_ablation"])

    def test_old_signal_target_and_format_are_rejected(self):
        value = self.signals()[0]
        for updates in ({"schema_version": "finresearchops.quant-signal/v1"},
                        {"target_id": "cn-a-industry-loo-equalweight-20d-percentile/v1"}):
            with self.assertRaisesRegex(ContractError, "VERSION_INVALID"):
                validate_signal({**value, **updates})

    def test_run_persists_and_restores_model_without_overwriting(self):
        result = run_experiment(self.data, self.spec, self.window,
                                NearestNeighborsModel(ablation="stock-only"), artifact_root=self.output)
        manifest, documents = read_run(self.output)
        self.assertTrue(all("schema_version" in document for document in documents.values()))
        self.assertEqual(result["dataset_id"], manifest["dataset_id"])
        self.assertEqual("NOT_STARTED", manifest["investment_effectiveness"])
        self.assertEqual("SYNTHETIC", result["data_kind"])
        self.assertEqual("DEFERRED", result["portfolio"]["status"])
        self.assertEqual("stock-only", result["feature_ablation"])
        recovered_data = from_document(documents["input.json"])
        self.assertEqual(self.data, recovered_data)
        model = restore_reference_model(documents["model.json"])
        predictions = model.predict(self.fold.test.rows)
        rebuilt = build_signals(predictions, self.dataset.panel, as_ofs=self.as_ofs,
                               model=model, dataset_id=self.dataset.dataset_id)
        self.assertEqual(list(rebuilt), result["signals"])
        with self.assertRaises(FileExistsError):
            run_experiment(self.data, self.spec, self.window, MeanModel(), artifact_root=self.output)

    def test_modified_artifact_content_is_detected(self):
        write_run(self.output, {"sample.json": {"schema_version": "quant.test/v1", "value": 1}},
                  data_kind="SYNTHETIC", dataset_id="a" * 64)
        (self.output / "sample.json").write_text('{"value":2}')
        with self.assertRaisesRegex(ContractError, "ARTIFACT_CONTENT"):
            read_run(self.output)

    def test_public_output_and_path_traversal_are_rejected_before_writing(self):
        with self.assertRaisesRegex(ContractError, "PRIVATE_DIRECTORY"):
            write_run(self.workspace / "finaudit-gate/run", {"sample.json": {}},
                      data_kind="SYNTHETIC", dataset_id="a" * 64)
        with self.assertRaisesRegex(ContractError, "FILENAME"):
            write_run(self.output, {"../outside.json": {}}, data_kind="SYNTHETIC", dataset_id="a" * 64)
        self.assertFalse(self.output.exists())

    def test_symlink_escape_from_private_directory_is_rejected(self):
        (self.workspace / "private").mkdir()
        (self.workspace / "private/link").symlink_to(self.workspace / "finaudit-gate", target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "PRIVATE_DIRECTORY"):
            write_run(self.workspace / "private/link/run", {"sample.json": {}},
                      data_kind="SYNTHETIC", dataset_id="a" * 64)

    def test_import_and_smoke_require_no_site_packages_or_core_package(self):
        target = self.workspace / "private/cli"
        completed = subprocess.run([sys.executable, "-S", "-m", "quant", "smoke",
            "--model", "mean", "--ablation", "stock-only", "--artifact-root", str(target)], cwd=ROOT, text=True,
            capture_output=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual("SYNTHETIC", receipt["data_kind"])
        self.assertEqual("stock-only", receipt["feature_ablation"])
        self.assertEqual("NOT_STARTED", receipt["claims"]["investment_effectiveness"])


if __name__ == "__main__":
    unittest.main()
