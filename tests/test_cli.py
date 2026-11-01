"""`grpc-coverage snapshot` and `clean` without pytest; `report` is covered in test_report.py."""

import json

from helpers import run_cli, write_project


def project(root, servers, extra=""):
    return write_project(
        root,
        f"""
        [tool.grpc-coverage]
        [tool.grpc-coverage.account]
        target = "{servers["v1"].target}"
        [tool.grpc-coverage.billing]
        target = "{servers["v1alpha"].target}"
        [tool.grpc-coverage.local]
        {extra}
        """,
    )


def taken_at(root, server):
    return json.loads((root / "grpc-coverage-results" / "schema" / f"{server}.json").read_text())["taken_at"]


def test_snapshot_without_pytest(tmp_path, servers):
    root = project(tmp_path, servers)
    result = run_cli(["snapshot"], root)
    assert result.returncode == 0, result.stderr
    assert "account: snapshot taken (reflection v1, 3 service(s)" in result.stdout
    assert "billing: snapshot taken (reflection v1alpha, 2 service(s)" in result.stdout
    assert "local: no target, skipped" in result.stdout
    meta = json.loads((root / "grpc-coverage-results" / "schema" / "account.json").read_text())
    assert meta["taken_by"] == "cli"


def test_repeat_run_retakes_nothing_force_retakes(tmp_path, servers):
    root = project(tmp_path, servers)
    run_cli(["snapshot"], root)
    first = taken_at(root, "account")
    again = run_cli(["snapshot"], root)
    assert again.stdout.count("snapshot exists, skipped") == 2
    assert taken_at(root, "account") == first
    forced = run_cli(["snapshot", "account", "--force"], root)
    assert "account: snapshot taken" in forced.stdout and "billing" not in forced.stdout


def test_snapshot_errors(tmp_path, servers):
    root = project(tmp_path, servers)
    assert run_cli(["snapshot", "nope"], root).returncode == 2
    no_target = run_cli(["snapshot", "local"], root)
    assert no_target.returncode == 2 and "coverage.snapshot(channel)" in no_target.stderr
    down = write_project(
        tmp_path / "down",
        '[tool.grpc-coverage]\nreflection-timeout = 1\n[tool.grpc-coverage.account]\ntarget = "127.0.0.1:1"\n',
    )
    failed = run_cli(["snapshot"], down)
    assert failed.returncode == 1 and "account: snapshot failed: UNAVAILABLE" in failed.stdout


def test_snapshot_then_report_from_a_subfolder(tmp_path, servers):
    root = project(tmp_path, servers)
    (root / "tests").mkdir()
    run_cli(["snapshot"], root / "tests")
    report = run_cli(["report", "--server", "account", "--server", "billing"], root / "tests")
    assert report.returncode == 0, report.stderr
    assert (root / "grpc-coverage-report" / "index.html").exists()
    assert "account   methods 0/" in report.stdout


def test_clean_removes_only_its_own_files(tmp_path, servers):
    root = project(tmp_path, servers)
    run_cli(["snapshot"], root)
    results = root / "grpc-coverage-results"
    (results / "notes.txt").write_text("mine")
    assert run_cli(["clean"], root).returncode == 0
    assert sorted(p.name for p in results.iterdir()) == ["notes.txt"]
    (results / "notes.txt").unlink()
    run_cli(["snapshot", "account"], root)
    run_cli(["clean"], root)
    assert not results.exists()


def test_version(tmp_path):
    from grpc_coverage import __version__

    assert run_cli(["--version"], tmp_path).stdout.strip() == f"grpc-coverage {__version__}"
