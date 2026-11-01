"""Start two local gRPC servers, run the demo tests against them and build the report.

    python examples/demo/run.py [--project DIR]

The servers are the test servers of this repository (`tests/helpers.py`) on 127.0.0.1 with random ports; their
stubs are generated from `tests/protos` into a temporary folder. Needs `grpcio-tools` (the `[proto]` extra).
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parents[1] / "tests"
sys.path.insert(0, str(TESTS))
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")  # before grpc is imported: no fork() notes from the servers

from helpers import TestServer, generate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=Path, default=HERE, help="the test project (default: examples/demo)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as gen:
        os.environ["GC_GEN_DIR"] = gen
        generate(Path(gen))
        account, billing = TestServer("account"), TestServer("billing")
        env = {
            **os.environ,
            "PYTHONPATH": gen,
            "ACCOUNT_TARGET": account.target,
            "BILLING_TARGET": billing.target,
        }
        try:
            for command in (
                ["grpc_coverage.cli", "clean"],
                ["pytest", "-p", "no:cacheprovider"],
                ["grpc_coverage.cli", "report"],
            ):
                code = subprocess.run([sys.executable, "-m", *command], cwd=args.project, env=env, check=False)
                if code.returncode != 0:
                    return code.returncode
        finally:
            account.stop()
            billing.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
