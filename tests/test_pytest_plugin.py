"""The plugin in real pytest runs (subprocess): many servers, deferred snapshots, xdist, report-on-finish, paths."""

import importlib.util
import json

import pytest
from helpers import run_pytest, write_project

# the CI job without pytest-xdist runs everything else in this file
needs_xdist = pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="pytest-xdist is not installed")

CONFTEST = """
import os

import grpc
import pytest
from helpers import Stubs, modules, service

from grpc_coverage import CoverageObserver

account_pb, billing_pb, common_pb = modules()


def channel_to(server, target_env):
    coverage = CoverageObserver(server)
    raw = grpc.insecure_channel(os.environ[target_env])
    return raw, coverage, grpc.intercept_channel(raw, coverage.interceptor())


@pytest.fixture(scope="session")
def account():
    raw, coverage, channel = channel_to("account", "ACCOUNT_TARGET")
    yield Stubs(channel, service(account_pb, "Account"))
    raw.close()


@pytest.fixture(scope="session")
def billing():
    raw, coverage, channel = channel_to("billing", "BILLING_TARGET")
    yield Stubs(channel, service(billing_pb, "Billing"))
    raw.close()
"""

TESTS = """
from conftest import account_pb, billing_pb


def test_get(account):
    assert account.Get(account_pb.GetRequest(id="1")).name == "1"


def test_create(account):
    account.Create(account_pb.CreateRequest(user=account_pb.User(name="ann")))


def test_list(account):
    assert len(list(account.List(account_pb.ListRequest(limit=3)))) == 3


def test_charge(billing):
    billing.Charge(billing_pb.ChargeRequest(amount=billing_pb.Money(units=1)))
"""


def env_for(servers, **extra):
    return {"ACCOUNT_TARGET": servers["v1"].target, "BILLING_TARGET": servers["v1alpha"].target, **extra}


def project(root, toml, files=None):
    return write_project(
        root,
        "[tool.grpc-coverage]\n" + toml,
        {"tests/conftest.py": CONFTEST, "tests/test_api.py": TESTS, **(files or {})},
    )


def calls(root):
    rows = []
    for path in (root / "grpc-coverage-results" / "calls").glob("*.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text().splitlines()[1:])
    return rows


def coverage_json(root):
    return json.loads((root / "grpc-coverage-report" / "coverage.json").read_text())


def by_name(data):
    return {server["name"]: server for server in data["servers"]}


def check(result, code=0):
    assert result.returncode == code, result.stdout + result.stderr
    return result.stdout


MANY = """
report-on-finish = true
[tool.grpc-coverage.account]
target = "${ACCOUNT_TARGET}"
[tool.grpc-coverage.billing]
target = "${BILLING_TARGET}"
[tool.grpc-coverage.notifications]
target = "${ACCOUNT_TARGET}"
include = ["account.v1.Admin"]
"""


def test_many_servers_snapshot_in_parallel_and_uncalled_server_is_zero(tmp_path, servers):
    root = project(tmp_path, MANY)
    out = check(run_pytest(["-k", "not charge"], root, env_for(servers)))
    assert "grpc-coverage: schema snapshots by target in" in out and "account, billing, notifications" in out
    assert "grpc-coverage" in out and "notifications" in out
    data = by_name(coverage_json(root))
    assert data["account"]["methods"]["covered"] == 3
    assert data["billing"]["called"] is False and data["billing"]["methods"]["percent"] == 0.0
    assert data["notifications"]["methods"] == {"covered": 0, "total": 1, "percent": 0.0}
    for server in data.values():
        assert server["schema"]["taken_by"] == "plugin"


def test_autouse_snapshot_fixture_with_k(tmp_path, servers):
    """A server without `target`: an autouse fixture takes the snapshot, even if `-k` leaves no test calling it."""
    root = project(
        tmp_path,
        "report-on-finish = true\n[tool.grpc-coverage.account]\n[tool.grpc-coverage.billing]\n",
        {
            "tests/conftest.py": CONFTEST
            + """

@pytest.fixture(scope="session", autouse=True)
def schemas():
    for name, env in (("account", "ACCOUNT_TARGET"), ("billing", "BILLING_TARGET")):
        raw, coverage, channel = channel_to(name, env)
        coverage.snapshot(channel)
        raw.close()
"""
        },
    )
    check(run_pytest(["-q", "-k", "test_get"], root, env_for(servers)))
    data = by_name(coverage_json(root))
    assert data["account"]["schema"]["taken_by"] == "code" and data["account"]["calls"] == 1
    assert data["billing"]["schema"]["error"] is None and data["billing"]["called"] is False


def test_server_started_by_a_fixture(tmp_path, servers):
    """The target is not known before the session: the plugin snapshot is deferred, `snapshot(channel)` takes it."""
    root = project(
        tmp_path,
        'report-on-finish = true\n[tool.grpc-coverage.account]\ntarget = "${LATE_TARGET}"\n',
        {
            "tests/conftest.py": """
                import grpc
                import pytest
                from helpers import Stubs, TestServer, modules, service

                from grpc_coverage import CoverageObserver

                account_pb, billing_pb, _ = modules()


                @pytest.fixture(scope="session")
                def account():
                    server = TestServer("account", "v1")
                    coverage = CoverageObserver("account")
                    with grpc.insecure_channel(server.target) as raw:
                        channel = grpc.intercept_channel(raw, coverage.interceptor())
                        assert coverage.snapshot(channel).status == "taken"
                        yield Stubs(channel, service(account_pb, "Account"))
                    server.stop()
                """,
            "tests/test_api.py": TESTS.replace("def test_charge(billing)", "def _charge(billing)"),
        },
    )
    out = check(run_pytest(["-q"], root, env_for(servers)))
    assert "WARNING account: ENV:" in out and "LATE_TARGET is not set" in out and "deferred" in out
    data = by_name(coverage_json(root))
    assert data["account"]["schema"]["taken_by"] == "code" and data["account"]["methods"]["covered"] == 3
    assert not (root / "grpc-coverage-results" / "schema" / "account.error").exists()


def test_errors_are_independent(tmp_path, servers):
    root = project(
        tmp_path,
        "report-on-finish = true\nreflection-timeout = 2\n"
        '[tool.grpc-coverage.account]\ntarget = "${ACCOUNT_TARGET}"\n'
        '[tool.grpc-coverage.billing]\ntarget = "127.0.0.1:1"\n',
    )
    out = check(run_pytest(["-q", "-k", "not charge"], root, env_for(servers)))
    assert "WARNING billing: UNAVAILABLE" in out
    data = by_name(coverage_json(root))
    assert data["account"]["schema"]["error"] is None and data["account"]["methods"]["covered"] == 3
    assert data["billing"]["schema"]["error"]["code"] == "UNAVAILABLE"
    assert "FAIL billing: no schema (UNAVAILABLE)" in out


@needs_xdist
def test_xdist_one_snapshot_per_server_all_calls_seen(tmp_path, servers):
    root = project(tmp_path, MANY.replace("report-on-finish = true", ""))
    out = check(run_pytest(["-q", "-n", "4"], root, env_for(servers)))
    assert "4 passed" in out
    assert sorted(c["method"].rsplit("/", 1)[1] for c in calls(root)) == ["Charge", "Create", "Get", "List"]
    schema = root / "grpc-coverage-results" / "schema"
    assert sorted(p.name for p in schema.iterdir()) == [
        f"{server}.{ext}" for server in ("account", "billing", "notifications") for ext in ("binpb", "json")
    ]
    assert len(list((root / "grpc-coverage-results" / "calls").glob("*.jsonl"))) <= 4


@needs_xdist
def test_report_on_finish_under_xdist_keeps_the_exit_code(tmp_path, servers):
    failing = TESTS + "\n\ndef test_broken(account):\n    assert False\n"
    root = project(tmp_path, "fail-under = 100\n" + MANY, {"tests/test_api.py": failing})
    out = check(run_pytest(["-q", "-n", "4"], root, env_for(servers)), code=1)
    assert "1 failed, 4 passed" in out
    assert "FAIL account: methods" in out  # the summary shows it, the exit code is still the tests' own
    assert coverage_json(root)["calls"] == 4

    root_ok = project(tmp_path / "ok", "fail-under = 100\n" + MANY)
    check(run_pytest(["-q", "-n", "4"], root_ok, env_for(servers)), code=0)
    assert coverage_json(root_ok)["failures"]


def test_report_flag_and_off_by_default(tmp_path, servers):
    root = project(tmp_path, MANY.replace("report-on-finish = true", ""))
    check(run_pytest(["-q"], root, env_for(servers)))
    assert not (root / "grpc-coverage-report").exists()
    check(run_pytest(["-q", "--grpc-coverage-report"], root, env_for(servers)))
    assert coverage_json(root)["calls"] == 8  # both runs share the results dir until `grpc-coverage clean`


@pytest.mark.parametrize(
    "where, args",
    [
        ("root", []),
        ("tests", []),
        ("outside", ["{root}/tests"]),
        pytest.param("root", ["-n", "2"], marks=needs_xdist),
    ],
)
def test_results_land_in_the_project_wherever_pytest_starts(tmp_path, servers, where, args):
    root = project(tmp_path / "proj", MANY.replace("report-on-finish = true", ""))
    cwd = {"root": root, "tests": root / "tests", "outside": tmp_path}[where]
    out = check(run_pytest([a.format(root=root) for a in args], cwd, env_for(servers)))
    assert f"-> {root / 'grpc-coverage-results'}" in out
    assert len(calls(root)) == 4
    assert not (cwd / "grpc-coverage-results").exists() or cwd == root


def test_chdir_inside_a_test(tmp_path, servers):
    chdir = TESTS + (
        "\n\ndef test_chdir_then_call(account, tmp_path):\n"
        "    import os\n\n"
        "    os.chdir(tmp_path)\n"
        '    account.Get(account_pb.GetRequest(id="2"))\n'
    )
    root = project(tmp_path, MANY.replace("report-on-finish = true", ""), {"tests/test_api.py": chdir})
    check(run_pytest(["-q"], root, env_for(servers)))
    assert len(calls(root)) == 5


def test_inactive_without_the_section(tmp_path, servers):
    root = write_project(tmp_path, "[project]\nname = 'x'\n", {"tests/test_x.py": "def test_x():\n    pass\n"})
    out = check(run_pytest(["-q", "--grpc-coverage-report"], root))
    assert "grpc-coverage" not in out
    assert not (root / "grpc-coverage-results").exists()


def test_bad_config_is_a_usage_error(tmp_path):
    root = write_project(
        tmp_path, "[tool.grpc-coverage]\nfail-under = 'high'\n", {"tests/test_x.py": "def test_x():\n    pass\n"}
    )
    result = run_pytest(["-q"], root)
    assert result.returncode == 4
    assert "grpc-coverage: [tool.grpc-coverage] fail-under: expected a number" in result.stderr


def test_observer_created_at_conftest_import_with_another_pyproject_warns(tmp_path):
    """The observer exists before the plugin's pytest_configure: the plugin still checks it and says so."""
    other = write_project(tmp_path / "other", "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    root = write_project(
        tmp_path / "proj",
        "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n",
        {
            "tests/conftest.py": f"""
                from grpc_coverage import CoverageObserver

                coverage = CoverageObserver("account", config={str(other)!r})
                """,
            "tests/test_x.py": "def test_x():\n    pass\n",
        },
    )
    out = check(run_pytest(["-q"], root))
    warning = f"WARNING the observer of 'account' uses {other / 'pyproject.toml'}"
    assert warning in out and f"the pytest plugin uses {root / 'pyproject.toml'}" in out
