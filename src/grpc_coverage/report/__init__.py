"""Build the report from results dirs: `report-dir/index.html`, `report-dir/coverage.json` and a summary.

Used by `grpc-coverage report` and by the pytest plugin with `report-on-finish`; there is no other entry point.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grpc_coverage.config import Config, ConfigError
from grpc_coverage.report.compute import compute, pct
from grpc_coverage.report.html import render
from grpc_coverage.report.results import read_results

FORMATS = ("html", "json")


@dataclass
class Report:
    data: dict[str, Any]
    summary: str
    written: list[Path]

    @property
    def exit_code(self) -> int:
        """1 if a threshold is not met or a declared server has no schema, else 0."""
        return 1 if self.data["failures"] else 0


def summary(data: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for server in data["servers"]:
        if server["schema"]["error"]:
            error = server["schema"]["error"]
            rows.append([server["name"], f"no schema: {error['code']}", "", error["message"]])
            continue
        methods, contracts = server["methods"], server["contracts"]
        rows.append(
            [
                server["name"],
                f"methods {methods['covered']}/{methods['total']} ({pct(methods)})",
                f"contracts {contracts['covered']}/{contracts['total']} ({pct(contracts)})",
                "" if server["called"] else "not called",
            ]
        )
    methods, contracts = data["totals"]["methods"], data["totals"]["contracts"]
    rows.append(
        [
            "total",
            f"methods {methods['covered']}/{methods['total']} ({pct(methods)})",
            f"contracts {contracts['covered']}/{contracts['total']} ({pct(contracts)})",
            "",
        ]
    )
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = [
        "   ".join(cell.ljust(widths[i]) if i < 3 else cell for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    lines += [f"FAIL {failure}" for failure in data["failures"]]
    return "\n".join(lines)


def build_report(
    config: Config,
    results_dirs: list[Path] | None = None,
    *,
    report_dir: Path | None = None,
    servers: list[str] | None = None,
    formats: tuple[str, ...] = FORMATS,
) -> Report:
    unknown = [name for name in servers or [] if name not in config.servers]
    if unknown:
        raise ConfigError(
            f"--server: unknown server(s) {', '.join(unknown)}; declared: {', '.join(config.servers) or 'none'}"
        )
    bad = [fmt for fmt in formats if fmt not in FORMATS]
    if bad:
        raise ConfigError(f"--format: unknown format(s) {', '.join(bad)}; known: {', '.join(FORMATS)}")
    results = read_results(results_dirs or [config.results_dir])
    data = compute(config, results, servers)
    target = report_dir or config.report_dir
    target.mkdir(parents=True, exist_ok=True)
    written = []
    if "json" in formats:
        path = target / "coverage.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    if "html" in formats:
        path = target / "index.html"
        path.write_text(render(data), encoding="utf-8")
        written.append(path)
    return Report(data=data, summary=summary(data), written=written)
