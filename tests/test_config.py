"""Config discovery, validation, priority of sources and `${VAR}` substitution."""

import logging
import os
from pathlib import Path

import pytest
from helpers import write_project

from grpc_coverage import CoverageObserver
from grpc_coverage.config import ConfigError, find_pyproject, load_config

BASIC = """
[project]
name = "acme-platform"

[tool.grpc-coverage]

[tool.grpc-coverage.account]
"""


def test_defaults(tmp_path):
    write_project(tmp_path, BASIC)
    config = load_config(start=tmp_path, env={})
    assert config.pyproject == tmp_path / "pyproject.toml"
    assert config.results_dir == tmp_path / "grpc-coverage-results"
    assert config.report_dir == tmp_path / "grpc-coverage-report"
    assert config.title == "acme-platform"
    assert config.exclude == ["grpc.reflection.*", "grpc.health.*"]
    assert config.fail_under is None and config.fail_under_contracts is None
    assert config.reflection_timeout == 10
    assert config.report_on_finish is False
    assert list(config.servers) == ["account"]


def test_all_keys(tmp_path):
    write_project(
        tmp_path,
        """
        [tool.grpc-coverage]
        results-dir = "out/results"
        report-dir = "out/report"
        title = "Account platform"
        exclude = ["grpc.*"]
        fail-under = 80
        fail-under-contracts = 60.5
        reflection-timeout = 3
        report-on-finish = true

        [tool.grpc-coverage.account]
        target = "${ACCOUNT_GRPC_TARGET:-localhost:50051}"
        tls = true
        root-cert = "certs/ca.pem"
        metadata = { x-api-key = "${ACCOUNT_API_KEY}" }
        proto = "protos/account"
        proto-include = ["third_party/googleapis"]
        include = ["account.v1.*"]
        exclude = ["account.v1.Internal"]
        fail-under = 90
        fail-under-contracts = 50

        [tool.grpc-coverage.account.expected-codes]
        "/account.v1.Account/Create" = ["INVALID_ARGUMENT", "ALREADY_EXISTS"]
        """,
    )
    config = load_config(start=tmp_path, env={"ACCOUNT_API_KEY": "k-1"})
    assert config.results_dir == tmp_path / "out" / "results"
    assert config.report_dir == tmp_path / "out" / "report"
    assert (config.title, config.exclude, config.fail_under, config.fail_under_contracts) == (
        "Account platform",
        ["grpc.*"],
        80,
        60.5,
    )
    assert config.reflection_timeout == 3 and config.report_on_finish is True
    account = config.servers["account"]
    assert account.target == "localhost:50051"
    assert account.tls is True
    assert account.root_cert == tmp_path / "certs" / "ca.pem"
    assert account.metadata == {"x-api-key": "k-1"}
    assert account.proto == [tmp_path / "protos" / "account"]
    assert account.proto_include == [tmp_path / "third_party" / "googleapis"]
    assert account.include == ["account.v1.*"]
    assert config.server_exclude(account) == ["grpc.*", "account.v1.Internal"]
    assert (account.fail_under, account.fail_under_contracts) == (90, 50)
    assert account.expected_codes == {"/account.v1.Account/Create": ["INVALID_ARGUMENT", "ALREADY_EXISTS"]}
    assert account.env_errors == {}


def test_servers_keep_table_order(tmp_path):
    write_project(
        tmp_path,
        """
        [tool.grpc-coverage]
        [tool.grpc-coverage.zeta]
        [tool.grpc-coverage.alpha]
        target = "a:1"
        [tool.grpc-coverage.mid-1_x]
        """,
    )
    assert list(load_config(start=tmp_path, env={}).servers) == ["zeta", "alpha", "mid-1_x"]


@pytest.mark.parametrize(
    "toml, message",
    [
        ("fail_under = 80", "fail_under: unknown key"),
        ("fail-under = '80'", "fail-under: expected a number"),
        ("fail-under = 120", "fail-under: expected a number from 0 to 100"),
        ("report-on-finish = 1", "report-on-finish: expected true or false"),
        ("exclude = 'grpc.*'", "exclude: expected a list of strings"),
        ("target = 'x:1'", "target: this key belongs to a server table"),
        ("[tool.grpc-coverage.title]", "clashes with the common key 'title'"),
        ("[tool.grpc-coverage.expected-codes]", "[tool.grpc-coverage.<server>.expected-codes]"),
        ("[tool.grpc-coverage.target]", "'target' is a server key, not a server"),
        ("[tool.grpc-coverage.account]\ntarge = 'x'", "[tool.grpc-coverage.account] targe: unknown key"),
        ("[tool.grpc-coverage.account]\ntls = 'yes'", "[tool.grpc-coverage.account] tls: expected true or false"),
        (
            "[tool.grpc-coverage.account.expected-codes]\n'/a.B/C' = ['NOPE']",
            "unknown status codes ['NOPE']",
        ),
        ("[tool.grpc-coverage.account.expected-codes]\n'Create' = ['NOT_FOUND']", "expected a full method name"),
        ('[tool.grpc-coverage."bad name"]', "server name may contain only"),
    ],
)
def test_invalid_config_names_the_key(tmp_path, toml, message):
    write_project(tmp_path, "[tool.grpc-coverage]\n" + toml + "\n")
    with pytest.raises(ConfigError) as error:
        load_config(start=tmp_path, env={})
    assert message in str(error.value)


def test_table_without_target_is_fine(tmp_path):
    write_project(tmp_path, BASIC)
    account = load_config(start=tmp_path, env={}).servers["account"]
    assert account.target is None and account.snapshot_error is None


# ---------------------------------------------------------------------------- discovery


def test_pyproject_without_section_is_skipped(tmp_path):
    write_project(tmp_path, BASIC)
    nested = write_project(tmp_path / "packages" / "inner", "[project]\nname = 'inner'\n")
    (nested / "tests").mkdir()
    assert find_pyproject(nested / "tests") == tmp_path / "pyproject.toml"
    assert load_config(start=nested / "tests", env={}).root == tmp_path


def test_not_found_falls_back_to_start_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="grpc_coverage"):
        config = load_config(start=tmp_path, env={})
    assert config.pyproject is None and config.root == tmp_path
    assert config.results_dir == tmp_path / "grpc-coverage-results"
    assert f"no pyproject.toml with [tool.grpc-coverage] above {tmp_path}" in caplog.text


def test_explicit_config_file_or_folder(tmp_path):
    write_project(tmp_path / "proj", BASIC)
    assert load_config(tmp_path / "proj" / "pyproject.toml").root == tmp_path / "proj"
    assert load_config(tmp_path / "proj").root == tmp_path / "proj"
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "missing.toml")


def test_observer_finds_config_from_its_own_file_not_cwd(tmp_path, monkeypatch):
    """The observer is created in a conftest deep in the project, cwd is somewhere else entirely."""
    project = write_project(
        tmp_path / "proj",
        BASIC,
        {
            "tests/sub/make.py": """
            from grpc_coverage import CoverageObserver

            def make():
                return CoverageObserver("account")
            """
        },
    )
    elsewhere = tmp_path / "elsewhere"
    write_project(elsewhere, "[tool.grpc-coverage]\n[tool.grpc-coverage.other]\n")
    monkeypatch.chdir(elsewhere)
    namespace: dict = {}
    source = project / "tests" / "sub" / "make.py"
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    observer = namespace["make"]()
    assert observer.config.pyproject == project / "pyproject.toml"
    assert observer.results_dir == project / "grpc-coverage-results"


def test_observer_explicit_config(tmp_path):
    write_project(tmp_path / "p", BASIC)
    observer = CoverageObserver("account", config=tmp_path / "p" / "pyproject.toml")
    assert observer.results_dir == tmp_path / "p" / "grpc-coverage-results"


def test_unknown_server_lists_declared(tmp_path):
    write_project(tmp_path, BASIC + "[tool.grpc-coverage.billing]\n")
    with pytest.raises(ValueError, match=r"unknown server 'acount'.*: account, billing"):
        CoverageObserver("acount", config=tmp_path)


def test_paths_are_absolute_once_chdir_does_not_matter(tmp_path, monkeypatch):
    write_project(tmp_path / "p", "[tool.grpc-coverage]\nresults-dir = 'rel/results'\n[tool.grpc-coverage.account]\n")
    observer = CoverageObserver("account", config=tmp_path / "p")
    monkeypatch.chdir(tmp_path)
    os.chdir(tmp_path)
    assert observer.results_dir == tmp_path / "p" / "rel" / "results"
    assert observer.results_dir.is_absolute()


def test_plugin_and_observer_disagree_warns(tmp_path, caplog):
    from grpc_coverage import _session

    write_project(tmp_path / "a", BASIC)
    write_project(tmp_path / "b", BASIC)
    _session.plugin_config = tmp_path / "b" / "pyproject.toml"
    with caplog.at_level(logging.WARNING, logger="grpc_coverage"):
        CoverageObserver("account", config=tmp_path / "a")
    assert str(tmp_path / "a" / "pyproject.toml") in caplog.text
    assert str(tmp_path / "b" / "pyproject.toml") in caplog.text


# ---------------------------------------------------------------------------- priority


def test_priority_flag_env_toml_default(tmp_path):
    write_project(
        tmp_path,
        """
        [tool.grpc-coverage]
        results-dir = "toml-results"
        report-dir = "toml-report"
        fail-under = 50
        [tool.grpc-coverage.account]
        """,
    )
    toml = load_config(start=tmp_path, env={})
    assert (toml.results_dir.name, toml.report_dir.name, toml.fail_under) == ("toml-results", "toml-report", 50)

    env = {
        "GRPC_COVERAGE_RESULTS_DIR": "env-results",
        "GRPC_COVERAGE_REPORT_DIR": str(tmp_path / "abs-report"),
        "GRPC_COVERAGE_FAIL_UNDER": "70",
    }
    from_env = load_config(start=tmp_path / "pyproject.toml", env=env)
    # a relative path from env is taken from the project root, not cwd; an absolute one as is
    assert from_env.results_dir == tmp_path / "env-results"
    assert from_env.report_dir == tmp_path / "abs-report"
    assert from_env.fail_under == 70

    flags = load_config(start=tmp_path, env=env, results_dir=tmp_path / "flag", report_dir="flag-rep", fail_under=90)
    assert flags.results_dir == tmp_path / "flag"
    assert flags.report_dir == Path("flag-rep").resolve()  # relative to cwd: the user typed it
    assert flags.fail_under == 90

    write_project(tmp_path / "d", "[tool.grpc-coverage]\n")
    defaults = load_config(start=tmp_path / "d", env={})
    assert defaults.results_dir == tmp_path / "d" / "grpc-coverage-results" and defaults.fail_under is None


def test_bad_fail_under_env(tmp_path):
    write_project(tmp_path, BASIC)
    with pytest.raises(ConfigError, match="GRPC_COVERAGE_FAIL_UNDER"):
        load_config(start=tmp_path, env={"GRPC_COVERAGE_FAIL_UNDER": "lots"})


# ---------------------------------------------------------------------------- env substitution


def test_env_substitution(tmp_path):
    write_project(
        tmp_path,
        """
        [tool.grpc-coverage]
        [tool.grpc-coverage.account]
        target = "${HOST}:${PORT:-50051}"
        metadata = { x-tenant = "${TENANT:-acme}", x-api-key = "${KEY}" }
        [tool.grpc-coverage.billing]
        target = "${BILLING_TARGET}"
        [tool.grpc-coverage.notifications]
        target = "${NOTIFY:-}"
        """,
    )
    config = load_config(start=tmp_path, env={"HOST": "stand", "KEY": "secret"})
    account = config.servers["account"]
    assert account.target == "stand:50051"
    assert account.metadata == {"x-tenant": "acme", "x-api-key": "secret"}
    # an unset variable without a default breaks only its own server, not the config
    billing = config.servers["billing"]
    assert billing.target is None
    assert (
        billing.snapshot_error == "[tool.grpc-coverage.billing] target: environment variable BILLING_TARGET is not set"
    )
    assert config.servers["notifications"].target == ""
    assert account.snapshot_error is None


def test_relative_co_filename_does_not_use_the_cwd(tmp_path, monkeypatch):
    """A module imported through a relative sys.path entry has a relative `co_filename`; `__file__` is absolute."""
    root = write_project(tmp_path / "proj", BASIC)
    conftest = root / "tests" / "conftest.py"
    conftest.parent.mkdir()
    monkeypatch.chdir(tmp_path)  # cwd has no pyproject.toml with the section
    code = compile("observer = CoverageObserver('account')", "tests/conftest.py", "exec")
    namespace = {"__file__": str(conftest), "CoverageObserver": CoverageObserver}
    exec(code, namespace)
    assert namespace["observer"].config.pyproject == root / "pyproject.toml"
