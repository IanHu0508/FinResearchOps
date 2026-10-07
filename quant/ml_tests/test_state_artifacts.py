from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from quant.state_artifacts import save_arrays


class StateArtifactTests(unittest.TestCase):
    def test_interrupted_arrays_are_preserved_and_a_complete_result_is_published(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "date.npz"
            raw = b"incomplete synthetic ZIP output"
            path.write_bytes(raw)
            save_arrays(path, {"scores": np.array([.2, .8])})
            preserved = list(Path(folder).glob("date.npz.interrupted-*") )
            evidence = next(p for p in preserved if not p.name.endswith(".json"))
            self.assertEqual(raw, evidence.read_bytes())
            with np.load(path, allow_pickle=False) as saved:
                np.testing.assert_array_equal(saved["scores"], [.2, .8])
            self.assertEqual([], list(Path(folder).glob("*.pending-*")))

    def test_a_readable_but_different_result_is_not_replaced_or_quarantined(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "date.npz"
            save_arrays(path, {"scores": np.array([.2, .8])})
            original = path.read_bytes()
            with self.assertRaises(AssertionError):
                save_arrays(path, {"scores": np.array([.3, .8])})
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], list(Path(folder).glob("*.interrupted-*")))

    def test_compression_error_in_an_unsealed_result_is_preserved_before_recomputation(self):
        from unittest.mock import patch
        from zlib import error as CompressionError
        with TemporaryDirectory() as folder:
            path = Path(folder) / "date.npz"
            raw = b"synthetic compressed stream damaged by interruption"
            path.write_bytes(raw)
            with patch("numpy.load", side_effect=CompressionError("invalid block type")):
                save_arrays(path, {"scores": np.array([.2, .8])})
            preserved = next(p for p in Path(folder).glob("*.interrupted-*") if not p.name.endswith(".json"))
            self.assertEqual(raw, preserved.read_bytes())
            with np.load(path, allow_pickle=False) as saved:
                np.testing.assert_array_equal(saved["scores"], [.2, .8])
