"""`grpc-coverage report | snapshot | clean`.

The config is the nearest `pyproject.toml` with `[tool.grpc-coverage]` from the current directory upwards (or
`--config`), so the commands work from any subfolder of the project. Paths given on the command line are relative to
the current directory, paths from the config — to the project root.

Exit codes: 0 — ok; 1 — coverage below a threshold, a server without a schema, a failed snapshot; 2 — bad config.
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path

from grpc_coverage._version import __version__
from grpc_coverage.config import Config, ConfigError, load_config
from grpc_coverage.report import FORMATS, build_report
from grpc_coverage.report.results import ResultsError
from grpc_coverage.snapshot import schema_dir, snapshot_targets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grpc-coverage", description="gRPC API coverage of tests.")
    parser.add_argument("--version", action="version", version=f"grpc-coverage {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="build index.html and coverage.json from the results")
    report.add_argument(
        "results_dirs",
        nargs="*",
        metavar="RESULTS_DIR",
        help="results dirs to merge (default: results-dir from the config)",
    )
    report.add_argument("--report-dir", help="where to write the report (default: report-dir from the config)")
    report.add_argument("--server", action="append", metavar="NAME", help="only these servers (repeatable)")
    report.add_argument("--fail-under", type=float, metavar="N", help="minimum method coverage, %%")
    report.add_argument("--fail-under-contracts", type=float, metavar="N", help="minimum contract coverage, %%")
    report.add_argument("--format", default=",".join(FORMATS), help="html,json (default: both)")
    report.add_argument("--config", metavar="PATH", help="pyproject.toml with [tool.grpc-coverage]")

    snapshot = commands.add_parser("snapshot", help="take schemas of the servers with `target`, without pytest")
    snapshot.add_argument("servers", nargs="*", metavar="SERVER", help="servers to snapshot (default: all with target)")
    snapshot.add_argument("--force", action="store_true", help="retake existing snapshots")
    snapshot.add_argument("--config", metavar="PATH", help="pyproject.toml with [tool.grpc-coverage]")

    clean = commands.add_parser("clean", help="empty results-dir before a new test run")
    clean.add_argument("--config", metavar="PATH", help="pyproject.toml with [tool.grpc-coverage]")
    return parser


def _report(args: argparse.Namespace) -> int:
    config = load_config(
        args.config,
        report_dir=args.report_dir,
        fail_under=args.fail_under,
        fail_under_contracts=args.fail_under_contracts,
    )
    dirs = [Path(d).expanduser().resolve() for d in args.results_dirs] or None
    formats = tuple(fmt.strip() for fmt in args.format.split(",") if fmt.strip())
    report = build_report(config, dirs, servers=args.server, formats=formats)
    print(report.summary)
    for path in report.written:
        print(f"wrote {path}")
    return report.exit_code


def _snapshot(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    unknown = [name for name in args.servers if name not in config.servers]
    if unknown:
        raise ConfigError(f"unknown server(s) {', '.join(unknown)}; declared: {', '.join(config.servers) or 'none'}")
    chosen = [config.servers[name] for name in args.servers] if args.servers else list(config.servers.values())
    without = [s.name for s in chosen if s.target is None and "target" not in s.env_errors]
    if args.servers and without:
        raise ConfigError(
            f"{', '.join(without)}: no `target` in [tool.grpc-coverage.<server>]; such servers are snapshotted from "
            "the tests with `coverage.snapshot(channel)`"
        )
    with_target = [s for s in chosen if s.name not in without]
    outcomes = snapshot_targets(config, with_target, taken_by="cli", force=args.force)
    failed = False
    for outcome in outcomes:
        if outcome.status == "taken":
            print(
                f"{outcome.server}: snapshot taken (reflection {outcome.version}, {len(outcome.services)} "
                f"service(s), {outcome.elapsed:.2f}s)"
            )
        elif outcome.status == "exists":
            print(f"{outcome.server}: snapshot exists, skipped (--force to retake)")
        elif outcome.status == "reflection-disabled":
            print(f"{outcome.server}: reflection is disabled, the report will use `proto`")
        else:
            failed = True
            print(f"{outcome.server}: snapshot failed: {outcome.code}: {outcome.message}")
    for name in without:
        print(f"{name}: no target, skipped (snapshot it from the tests with coverage.snapshot(channel))")
    print(f"schemas in {schema_dir(config.results_dir)}")
    return 1 if failed else 0


def _clean(config: Config) -> int:
    """Remove only what grpc-coverage writes (`calls/`, `schema/`), never the folder content as a whole."""
    folder = config.results_dir
    for name in ("calls", "schema"):
        shutil.rmtree(folder / name, ignore_errors=True)
    try:
        folder.rmdir()
    except OSError:
        pass
    print(f"cleaned {folder}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = _parser().parse_args(argv)
    try:
        if args.command == "report":
            return _report(args)
        if args.command == "snapshot":
            return _snapshot(args)
        return _clean(load_config(args.config))
    except (ConfigError, ResultsError) as exc:
        print(f"grpc-coverage: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
