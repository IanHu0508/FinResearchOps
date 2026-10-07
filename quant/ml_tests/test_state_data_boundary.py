from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from quant.models.analogues import file_digest
from quant.state_data import CACHE_FILE, FEATURES, SCHEMA, StateInputs, timestamp_ns


class StateBoundaryTests(unittest.TestCase):
    def fixture(self, root):
        repo, private = root / "finaudit-gate", root / "private"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        inputs, output = private / "inputs", private / "cache"
        inputs.mkdir(parents=True)
        output.mkdir()
        np.save(output / "values.npy", np.ones((6, 14)))
        np.save(output / "returns.npy", np.asarray([-.08, .04, np.nan, -.01, .02, np.nan]))
        ns = timestamp_ns(datetime(2025, 1, 31, 10, tzinfo=timezone.utc))
        np.save(output / "available.npy", np.full(6, ns, dtype=np.int64))
        np.save(output / "days.npy", np.asarray([0, 0, 0, 1, 1, 1], dtype=np.int32))
        days = []
        for code, day in enumerate(("2025-01-01", "2025-01-02")):
            raw = json.dumps({"symbols": ["A", "B", "C"]}).encode()
            (output / (day + ".json")).write_bytes(raw)
            days.append({"date": day, "code": code, "start": code * 3, "stop": (code + 1) * 3,
                         "rows": 3, "symbols_sha256": sha256(raw).hexdigest(),
                         "label_end_date": "2025-01-30" if code == 0 else "2025-01-31"})
        manifest = {"schema_version": SCHEMA, "features": list(FEATURES), "rows": 6,
                    "days": days, "source_manifests": {},
                    "files": {x: file_digest(output / x) for x in
                              ("values.npy", "returns.npy", "available.npy", "days.npy")}}
        (output / CACHE_FILE).write_text(json.dumps(manifest))
        return inputs, output

    def test_swapped_member_identity_is_rejected_even_when_array_bytes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs, output = self.fixture(Path(tmp))
            value = StateInputs(inputs, output)
            self.assertEqual(["A", "B", "C"], value.symbols("2025-01-01"))
            (output / "2025-01-01.json").write_text(json.dumps({"symbols": ["B", "A", "C"]}))
            with self.assertRaisesRegex(ValueError, "SYMBOL_CONTENT"):
                StateInputs(inputs, output)
            with self.assertRaisesRegex(ValueError, "SYMBOL_CONTENT"):
                value.symbols("2025-01-01")

    def test_end_date_must_precede_shanghai_cutoff_even_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs, output = self.fixture(Path(tmp))
            value = StateInputs(inputs, output)
            before = value.supervision("2025-01-31T23:00:00+08:00")
            np.testing.assert_array_equal(before["ids"], [0, 1])
            np.testing.assert_allclose(before["risk"], [.08, 0])
            np.testing.assert_allclose(before["targets"], [[0, .5], [.5, 1]])
            later = value.supervision("2025-02-01T00:00:00+08:00")
            np.testing.assert_array_equal(later["ids"], [0, 1, 3, 4])
            same_shanghai_date = value.supervision("2025-01-31T15:00:00+00:00")
            np.testing.assert_array_equal(same_shanghai_date["ids"], [0, 1])
