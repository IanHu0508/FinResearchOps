"""Offline, synthetic-only CLI. Live acquisition and model training are separate."""

import argparse
import json
import socket

from quant.models.baseline import MeanModel, NearestNeighborsModel
from quant.contracts import ABLATIONS
from quant.models.views import VIEWS
from quant.pipeline import run_experiment
from quant.synthetic import make_synthetic_data, synthetic_fold


def main():
    parser = argparse.ArgumentParser(description="Market-only Quant V1 synthetic smoke test")
    parser.add_argument("command", choices=("smoke",))
    parser.add_argument("--artifact-root", required=True, help="New run directory under sibling private/")
    parser.add_argument("--model", choices=("neighbors", "mean"), default="neighbors")
    parser.add_argument("--ablation", choices=ABLATIONS, default="stock+context")
    parser.add_argument("--view", choices=VIEWS, default="tabular")
    args = parser.parse_args()
    def blocked(*args, **kwargs):
        raise RuntimeError("NETWORK_DISABLED_IN_SYNTHETIC_SMOKE")
    # No providers are imported. Also reject accidental Python socket connections.
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    data, spec = make_synthetic_data()
    model = (NearestNeighborsModel(view=args.view, ablation=args.ablation) if args.model == "neighbors"
             else MeanModel(ablation=args.ablation))
    result = run_experiment(data, spec, synthetic_fold(data), model, artifact_root=args.artifact_root)
    print(json.dumps({"data_kind": result["data_kind"], "dataset_id": result["dataset_id"],
        "feature_ablation": result["feature_ablation"], "input_view": result["input_view"],
        "counts": result["counts"], "claims": result["claims"],
        "message": "Synthetic plumbing only; no investment-performance conclusion."}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
