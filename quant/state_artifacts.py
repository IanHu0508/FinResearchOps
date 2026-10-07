"""Atomic state-study array writes with preserved interrupted-output evidence."""

import os
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile
from zlib import error as CompressionError

from quant.contracts import canonical, require
from quant.models.analogues import file_digest


def save_arrays(path, arrays):
    import numpy as np
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with np.load(path, allow_pickle=False) as saved:
                existing = {key: saved[key].copy() for key in saved.files}
        except (OSError, EOFError, ValueError, BadZipFile, CompressionError) as error:
            digest = file_digest(path)
            archive = path.with_name(path.name + ".interrupted-" + digest)
            require(not archive.exists(), "STATE_INTERRUPTED_ARCHIVE_COLLISION")
            path.rename(archive)
            observation = path.with_name(path.name + ".interrupted-" + digest + ".json")
            observation.write_text(canonical({"schema_version": "quant.state-interrupted-array/v1",
                "original_name": path.name, "preserved_name": archive.name, "sha256": digest,
                "error_type": type(error).__name__, "replacement_policy": "same_frozen_inputs_only"}) + "\n")
        else:
            require(set(existing) == set(arrays), "STATE_UNSEALED_FORECAST_SHAPE")
            for key, value in arrays.items():
                np.testing.assert_array_equal(existing[key], value)
            return
    pending = path.with_name(path.name + ".pending-" + uuid4().hex + ".npz")
    np.savez_compressed(pending, **arrays)
    # Exclusive hard-link publication prevents a simultaneous writer from
    # replacing an existing result. A crash leaves its own pending file intact.
    os.link(pending, path)
    pending.unlink()
