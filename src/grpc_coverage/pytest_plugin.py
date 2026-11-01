"""pytest plugin (entry point `pytest11`): schemas by `target` at session start, `report-on-finish` at the end.

Active only if a `pyproject.toml` with `[tool.grpc-coverage]` is found from `rootdir` upwards. Both hooks run in the
xdist controller only (workers have `config.workerinput`): one snapshot per server, one report after all workers.
"""

import time
from typing import Any

import pytest

from grpc_coverage import _session
from grpc_coverage.config import Config, ConfigError, find_pyproject, load_config
from grpc_coverage.snapshot import has_snapshot, snapshot_targets

_CONFIG = pytest.StashKey[Config]()
_HEADER = pytest.StashKey[list[str]]()
_SUMMARY = pytest.StashKey[list[str]]()
_EARLY = pytest.StashKey[list[str]]()  # warnings found in pytest_configure


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("grpc-coverage")
    group.addoption(
        "--grpc-coverage-report",
        action="store_true",
        default=None,
        help="build the grpc-coverage report at the end of the session (report-on-finish)",
    )


def _controller(config: pytest.Config) -> bool:
    return not hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    pyproject = find_pyproject(config.rootpath)
    if pyproject is None:
        return
    try:
        coverage_config = load_config(pyproject)
    except ConfigError as exc:
        raise pytest.UsageError(f"grpc-coverage: {exc}") from exc
    config.stash[_CONFIG] = coverage_config
    config.stash[_EARLY] = _session.set_plugin_config(coverage_config.pyproject)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _CONFIG in config.stash:
        _session.set_plugin_config(None)


@pytest.hookimpl(tryfirst=True)  # before the terminal reporter prints the header with our lines
def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config.stash.get(_CONFIG, None)
    if config is None or not _controller(session.config):
        return
    lines: list[str] = list(session.config.stash.get(_EARLY, []))
    session.config.stash[_HEADER] = lines
    servers = [
        server
        for server in config.servers.values()
        if (server.target is not None or "target" in server.env_errors)
        and not has_snapshot(config.results_dir, server.name)
    ]
    if not servers:
        return
    started = time.monotonic()
    outcomes = snapshot_targets(config, servers, taken_by="plugin")
    taken = [o.server for o in outcomes if o.status == "taken"]
    summary = f"grpc-coverage: schema snapshots by target in {time.monotonic() - started:.2f}s"
    if taken:
        summary += f": {', '.join(taken)}"
    lines.append(summary)
    for outcome in outcomes:
        if outcome.status == "error":
            hint = " (deferred: coverage.snapshot(channel) in a fixture can still take it)"
            lines.append(f"grpc-coverage: WARNING {outcome.server}: {outcome.code}: {outcome.message}{hint}")
        elif outcome.status == "reflection-disabled":
            lines.append(f"grpc-coverage: {outcome.server}: reflection is disabled, the report will use `proto`")


def pytest_report_header(config: pytest.Config) -> list[str]:
    coverage_config = config.stash.get(_CONFIG, None)
    if coverage_config is None:
        return []
    return [
        f"grpc-coverage: {coverage_config.pyproject} -> {coverage_config.results_dir}",
        *config.stash.get(_HEADER, []),
    ]


def pytest_sessionfinish(session: pytest.Session, exitstatus: Any) -> None:
    config = session.config.stash.get(_CONFIG, None)
    if config is None or not _controller(session.config):
        return
    # The header is hidden under `-q`; snapshot problems must still be seen, so they go to the summary too.
    lines = session.config.stash.get(_HEADER, [])
    session.config.stash[_SUMMARY] = [line for line in lines if not line.startswith("grpc-coverage: schema")]
    option = session.config.getoption("--grpc-coverage-report")
    if not (option if option is not None else config.report_on_finish):
        return
    from grpc_coverage.report import build_report

    # The same code as `grpc-coverage report`. Thresholds are not applied to the pytest exit code on purpose:
    # coverage below the bar is a separate CI step, not a test failure.
    try:
        report = build_report(config)
    except Exception as exc:  # the report must never turn a test run red
        session.config.stash[_SUMMARY].append(f"grpc-coverage: report failed: {exc}")
        return
    session.config.stash[_SUMMARY] += [*report.summary.splitlines(), *(f"wrote {path}" for path in report.written)]


def pytest_terminal_summary(terminalreporter: Any, exitstatus: Any, config: pytest.Config) -> None:
    lines = config.stash.get(_SUMMARY, None)
    if not lines:
        return
    terminalreporter.section("grpc-coverage")
    for line in lines:
        terminalreporter.write_line(line)
