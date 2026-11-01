"""Read one or more results dirs (`calls/*.jsonl`, `schema/*`) and merge them.

Merging is a union of sets and a sum of counters, so the order of the dirs never changes the result.
"""

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grpc_coverage._version import FORMAT_VERSION

logger = logging.getLogger("grpc_coverage")


class ResultsError(Exception):
    """Results that can't be read, e.g. written by an incompatible version of grpc-coverage."""


@dataclass
class MethodCalls:
    calls: int = 0
    kinds: set[str] = field(default_factory=set)
    codes: Counter[str] = field(default_factory=Counter)
    request_fields: set[str] = field(default_factory=set)
    enum_values: set[str] = field(default_factory=set)
    oneofs: set[str] = field(default_factory=set)


@dataclass
class Snapshot:
    folder: Path
    meta: dict[str, Any]
    data: bytes


@dataclass
class Results:
    dirs: list[Path]
    files: int = 0
    calls: int = 0
    # server -> method -> calls
    servers: dict[str, dict[str, MethodCalls]] = field(default_factory=dict)
    snapshots: dict[str, list[Snapshot]] = field(default_factory=dict)
    errors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reflection_disabled: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _major(version: Any) -> str:
    return str(version).split(".", 1)[0]


def read_results(dirs: list[Path]) -> Results:
    results = Results(dirs=dirs)
    for folder in sorted(set(dirs)):
        if not folder.is_dir():
            results.warnings.append(f"results dir {folder} does not exist")
            continue
        _read_calls(folder, results)
        _read_schema(folder, results)
    for warning in results.warnings:
        logger.warning("grpc-coverage: %s", warning)
    return results


def _read_calls(folder: Path, results: Results) -> None:
    calls_dir = folder / "calls"
    if not calls_dir.is_dir():
        return
    for path in sorted(calls_dir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix != ".jsonl":
            results.warnings.append(f"{path}: not a grpc-coverage results file, skipped")
            continue
        with path.open(encoding="utf-8", errors="replace") as file:
            first = file.readline()
            try:
                head = json.loads(first)
            except ValueError:
                head = None
            if not isinstance(head, dict) or head.get("type") != "header":
                results.warnings.append(f"{path}: no grpc-coverage header, skipped")
                continue
            if _major(head.get("format")) != _major(FORMAT_VERSION):
                raise ResultsError(
                    f"{path}: results format {head.get('format')} (grpc-coverage {head.get('grpc_coverage')}) is not "
                    f"supported by this grpc-coverage, which reads format {FORMAT_VERSION}; rerun the tests or use "
                    f"the same grpc-coverage version for the report"
                )
            results.files += 1
            broken = 0
            for line in file:
                try:
                    record = json.loads(line)
                    server, method, code = record["server"], record["method"], record["code"]
                except (ValueError, KeyError, TypeError):
                    broken += 1
                    continue
                entry = results.servers.setdefault(server, {}).setdefault(method, MethodCalls())
                entry.calls += 1
                results.calls += 1
                if record.get("kind"):
                    entry.kinds.add(record["kind"])
                entry.codes[code] += 1
                entry.request_fields.update(record.get("request_fields", ()))
                entry.enum_values.update(record.get("enum_values", ()))
                entry.oneofs.update(record.get("oneofs", ()))
            if broken:
                results.warnings.append(f"{path}: {broken} unreadable line(s) skipped")


def _read_json(path: Path, results: Results) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        results.warnings.append(f"{path}: unreadable ({exc}), skipped")
        return None
    return data if isinstance(data, dict) else None


def _read_schema(folder: Path, results: Results) -> None:
    schema_dir = folder / "schema"
    if not schema_dir.is_dir():
        return
    for path in sorted(schema_dir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        server = path.stem
        if path.suffix == ".json":
            binpb = path.with_suffix(".binpb")
            meta = _read_json(path, results)
            if meta is None:
                continue
            if _major(meta.get("format")) != _major(FORMAT_VERSION):
                raise ResultsError(f"{path}: snapshot format {meta.get('format')} is not supported")
            # A partial copy of a results dir (an interrupted artifact upload, a deleted file): the snapshot was
            # taken, so "no schema, take one" would be wrong. Shown as an error of this server instead.
            data = binpb.read_bytes() if binpb.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                problem = "is missing" if data is None else "does not match the sha256 in " + path.name
                results.errors.setdefault(server, []).append(
                    {
                        "code": "SNAPSHOT_INCOMPLETE",
                        "message": f"{binpb} {problem}; take the snapshot again: grpc-coverage snapshot --force "
                        f"{server}, or rerun the tests after grpc-coverage clean",
                        "taken_by": meta.get("taken_by"),
                        "target": meta.get("target"),
                        "time": meta.get("taken_at"),
                    }
                )
                continue
            results.snapshots.setdefault(server, []).append(Snapshot(folder, meta, data))
        elif path.suffix == ".error":
            marker = _read_json(path, results)
            if marker is not None:
                results.errors.setdefault(server, []).append(marker)
        elif path.suffix == ".reflection-disabled":
            marker = _read_json(path, results) or {}
            results.reflection_disabled.setdefault(server, []).append(marker)
