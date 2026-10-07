import gzip
from hashlib import sha256
from pathlib import Path
import pickle
import tempfile
import unittest

from datetime import datetime, timezone
from quant.state_data import downside_label, read_admitted, prepare, timestamp_ns


class Forbidden:
    def __reduce__(self):
        return (eval, ("1 + 2",))


class StateLabelTests(unittest.TestCase):
    def test_real_cache_cannot_be_created_in_a_public_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "finaudit-gate").mkdir()
            (root / "finaudit-gate/pyproject.toml").write_text("[project]\nname='fixture'\n")
            (root / "private").mkdir()
            with self.assertRaisesRegex(ValueError, "SIBLING_PRIVATE"):
                prepare(root / "private/inputs", root / "finaudit-gate/leak")
            self.assertFalse((root / "finaudit-gate/leak").exists())

    def test_time_conversion_preserves_subsecond_availability(self):
        value = datetime(1970, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(123456000, timestamp_ns(value))

    def test_downside_target_preserves_unknown_and_distinguishes_gain_from_loss(self):
        self.assertIsNone(downside_label(None))
        self.assertEqual(0, downside_label(.05))
        self.assertEqual(.08, downside_label(-.08))
        with self.assertRaises(ValueError):
            downside_label(True)

    def test_admitted_cache_checks_bytes_and_rejects_executable_globals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.pickle.gz"
            raw = gzip.compress(pickle.dumps(Forbidden()))
            path.write_bytes(raw)
            with self.assertRaises(pickle.UnpicklingError):
                read_admitted(path, sha256(raw).hexdigest())
            with self.assertRaisesRegex(ValueError, "CONTENT_MISMATCH"):
                read_admitted(path, "0" * 64)
