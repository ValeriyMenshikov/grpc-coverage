"""`examples/demo`: the project of the README screenshot builds its report."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"


@pytest.mark.skipif(importlib.util.find_spec("grpc_tools") is None, reason="run.py generates stubs with grpcio-tools")
def test_demo(tmp_path):
    project = tmp_path / "demo"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("grpc-coverage-*", "__pycache__", "run.py"))
    result = subprocess.run(
        [sys.executable, str(DEMO / "run.py"), "--project", str(project)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "9 passed" in result.stdout
    data = json.loads((project / "grpc-coverage-report" / "coverage.json").read_text())
    servers = {server["name"]: server for server in data["servers"]}
    assert list(servers) == ["account", "billing"]
    assert servers["account"]["methods"] == {"covered": 4, "total": 7, "percent": 57.14}
    assert servers["billing"]["methods"]["covered"] == 2 and data["failures"] == []
    assert (project / "grpc-coverage-report" / "index.html").is_file()
