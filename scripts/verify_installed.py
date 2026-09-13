"""Run the core suite from an installed wheel, with no source package available."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path, help="Python in a fresh environment containing the built wheel.")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    # Resolving a venv symlink would select the base interpreter; use its original path.
    interpreter = Path(os.path.abspath(args.python))
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    with tempfile.TemporaryDirectory(prefix="finresearchops-installed-") as directory:
        root = Path(directory)
        for name in ("tests", "fixtures", "schemas", "scripts", "manifests"):
            shutil.copytree(REPOSITORY / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPOSITORY / "pyproject.toml", root / "pyproject.toml")
        assert not (root / "src").exists()
        probe = subprocess.run([str(interpreter), "-I", "-c",
            "import pathlib,finauditgate; print(pathlib.Path(finauditgate.__file__).resolve().parent)"],
            cwd=root, env=env, check=True, capture_output=True, text=True)
        installed = Path(probe.stdout.strip())
        if installed.is_relative_to(REPOSITORY) or "site-packages" not in installed.parts:
            raise AssertionError("INSTALLED_PACKAGE_ORIGIN_INVALID")
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        source_files = {str(p.relative_to(REPOSITORY / "src/finauditgate")): digest(p)
                        for p in (REPOSITORY / "src/finauditgate").rglob("*.py")}
        installed_files = {str(p.relative_to(installed)): digest(p) for p in installed.rglob("*.py")}
        if source_files != installed_files:
            raise AssertionError("INSTALLED_PACKAGE_SOURCE_MISMATCH")
        tests = subprocess.run([str(interpreter), "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests"],
            cwd=root, env=env, capture_output=True, text=True)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        log = args.receipt.with_suffix(".log")
        log.write_text(tests.stdout + tests.stderr)
        receipt = {"status": "COMPLETED" if tests.returncode == 0 else "PARTIAL", "exit_code": tests.returncode,
                   "installed_package": str(installed), "python_module_files": len(source_files),
                   "source_package_absent_from_test_workspace": True, "pythonpath_removed": True,
                   "log_sha256": digest(log), "scope": "INSTALLED_CORE_ACCEPTANCE_NOT_FINANCIAL_EVALUATION"}
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(receipt, ensure_ascii=False))
        if tests.returncode:
            print(tests.stderr[-6000:])
            raise SystemExit(tests.returncode)


if __name__ == "__main__":
    main()
