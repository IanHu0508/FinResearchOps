"""Launch the separate Quant interpreter with its explicit native runtime."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


CHECK = """
import importlib.metadata as metadata
import json
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 12):
    raise SystemExit('Quant requires Python 3.12')
expected = {}
for line in Path('requirements/quant-ml.lock').read_text().splitlines():
    if line and not line.startswith('#'):
        name, version = line.split('==')
        expected[name] = version
actual = {name: metadata.version(name) for name in expected}
if actual != expected:
    raise SystemExit('Quant dependency versions differ from requirements/quant-ml.lock')
import numpy
import scipy
import xgboost
import quant.state_study
import quant.state_meta
import quant.state_packet
import quant.state_report
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])),
                  'packages': actual, 'imports': 'OK'}, sort_keys=True))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path,
                        help="Python executable in the isolated Quant environment")
    parser.add_argument("--openmp-lib", type=Path,
                        help="macOS libomp.dylib; defaults to <environment>/lib/libomp.dylib")
    parser.add_argument("--check", action="store_true",
                        help="check pinned dependencies and actual numerical imports")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Python arguments after --, for example -m quant.state_report ...")
    args = parser.parse_args()
    # Resolving the executable's symlink would discard the virtual environment.
    executable = args.python.expanduser().absolute()
    if not executable.is_file():
        parser.error("Quant Python executable does not exist")
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(PYTHONPATH=os.pathsep.join((str(repository), str(repository / "src"))),
                       PYTHONDONTWRITEBYTECODE="1", LANGSMITH_TRACING="false",
                       LANGCHAIN_TRACING_V2="false")
    if sys.platform == "darwin":
        library = (args.openmp_lib or executable.parent.parent / "lib/libomp.dylib").expanduser().absolute()
        if not library.is_file() or library.name != "libomp.dylib":
            parser.error("provide the installed macOS libomp.dylib with --openmp-lib")
        environment["DYLD_LIBRARY_PATH"] = str(library.parent)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.check and command:
        parser.error("--check does not take a command")
    if not args.check and not command:
        parser.error("provide --check or Python arguments after --")
    result = subprocess.run([str(executable), *( ["-c", CHECK] if args.check else command)],
                            cwd=repository, env=environment, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
