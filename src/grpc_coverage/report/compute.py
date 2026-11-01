"""Coverage numbers: methods and contracts per method, proto service, server and in total.

The result is a plain JSON-ready dict: it is written as `coverage.json` and embedded into `index.html` as is.
"""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import MethodDescriptor
from google.protobuf.message import DecodeError

from grpc_coverage._version import FORMAT_VERSION, __version__
from grpc_coverage.config import Config, ServerConfig
from grpc_coverage.paths import elements
from grpc_coverage.report.results import MethodCalls, Results
from grpc_coverage.schema import SchemaError, build_pool, compile_proto, find_service, selected

NO_SCHEMA_HINT = (
    "take it with `coverage.snapshot(channel)` in the fixture that creates the channel, set `target` in "
    "[tool.grpc-coverage.{server}] so the pytest plugin or `grpc-coverage snapshot` takes it, or set `proto`"
)


def ratio(covered: int, total: int) -> dict[str, Any]:
    return {
        "covered": covered,
        "total": total,
        "percent": round(covered * 100 / total, 2) if total else None,
    }


def _add(into: dict[str, int], part: dict[str, Any]) -> None:
    into["covered"] += part["covered"]
    into["total"] += part["total"]


def _kind(method: MethodDescriptor) -> str:
    request = "stream" if method.client_streaming else "unary"
    response = "stream" if method.server_streaming else "unary"
    return f"{request}_{response}"


def _sorted_codes(codes: Counter[str]) -> dict[str, int]:
    return dict(sorted(codes.items(), key=lambda item: (item[0] != "OK", -item[1], item[0])))


class _Schema:
    def __init__(self) -> None:
        self.source = "error"
        self.pool: Any = None
        self.services: list[str] = []
        self.excluded: list[str] = []
        self.error: dict[str, Any] | None = None
        self.meta: dict[str, Any] = {}


def _resolve_schema(config: Config, server: ServerConfig, results: Results) -> _Schema:
    """Snapshot > error marker > `proto` > nothing. With an error marker `proto` is *not* used: it may not match."""
    schema = _Schema()
    exclude = config.server_exclude(server)
    snapshots = results.snapshots.get(server.name, [])
    if snapshots:
        hashes = {snapshot.meta.get("sha256") for snapshot in snapshots}
        if len(hashes) > 1:
            described = "; ".join(
                f"{snapshot.folder}: target {snapshot.meta.get('target')}, taken {snapshot.meta.get('taken_at')}"
                for snapshot in sorted(snapshots, key=lambda s: str(s.folder))
            )
            schema.error = {
                "code": "SCHEMA_MISMATCH",
                "message": f"the results dirs hold different schema snapshots of this server ({described})",
            }
            return schema
        snapshot = min(snapshots, key=lambda s: str(s.folder))
        schema.meta = {key: snapshot.meta.get(key) for key in ("target", "taken_by", "taken_at", "sha256")}
        schema.source = f"reflection-{snapshot.meta.get('reflection', 'v1')}"
        try:
            file_set = descriptor_pb2.FileDescriptorSet.FromString(snapshot.data)
        except DecodeError as exc:
            schema.error = {"code": "SCHEMA", "message": f"{snapshot.folder}: unreadable schema snapshot: {exc}"}
            return schema
        listed = list(snapshot.meta.get("services", []))
        schema.excluded = list(snapshot.meta.get("excluded", []))
    elif server.name in results.errors:
        marker = sorted(results.errors[server.name], key=lambda m: str(m.get("time")))[-1]
        schema.error = {
            "code": marker.get("code", "UNKNOWN"),
            "message": marker.get("message", ""),
            "taken_by": marker.get("taken_by"),
            "target": marker.get("target"),
        }
        return schema
    elif server.proto or "proto" in server.env_errors:
        if "proto" in server.env_errors:
            schema.error = {"code": "ENV", "message": server.env_errors["proto"]}
            return schema
        try:
            file_set, listed = compile_proto(server.proto, server.proto_include)
        except SchemaError as exc:
            schema.error = {"code": "PROTO", "message": str(exc)}
            return schema
        schema.source = "proto"
        schema.meta = {"proto": [str(path) for path in server.proto]}
    elif server.name in results.reflection_disabled:
        schema.error = {
            "code": "REFLECTION_DISABLED",
            "message": f"reflection is disabled on the server and `proto` is not set in [tool.grpc-coverage."
            f"{server.name}]; point `proto` at the .proto files of the server",
        }
        return schema
    else:
        schema.error = {
            "code": "NO_SCHEMA",
            "message": "no schema snapshot: " + NO_SCHEMA_HINT.format(server=server.name),
        }
        return schema

    try:
        schema.pool = build_pool(file_set)
    except (SchemaError, TypeError, ValueError) as exc:
        schema.error = {"code": "SCHEMA", "message": f"broken schema: {exc}"}
        return schema
    schema.services = [name for name in listed if selected(name, server.include, exclude)]
    schema.excluded = sorted({*schema.excluded, *(name for name in listed if name not in schema.services)})
    return schema


def _method(server: ServerConfig, method: MethodDescriptor, calls: MethodCalls | None) -> dict[str, Any]:
    full = f"/{method.containing_service.full_name}/{method.name}"
    calls = calls or MethodCalls()
    contract = elements(method.input_type)
    expected = ["OK", *(code for code in server.expected_codes.get(full, []) if code != "OK")]
    fields = [
        {
            "path": element.path,
            "type": element.type,
            "covered": element.path in calls.request_fields,
            "implicit_default": element.implicit_default,
        }
        for element in contract.fields
    ]
    enums = [{"value": value, "covered": value in calls.enum_values} for value in contract.enum_values]
    oneofs = [{"branch": branch, "covered": branch in calls.oneofs} for branch in contract.oneofs]
    codes = [{"code": code, "covered": calls.codes.get(code, 0) > 0} for code in expected]
    items = [*fields, *enums, *oneofs, *codes]
    return {
        "name": method.name,
        "full_name": full,
        "kind": _kind(method),
        "request_type": method.input_type.full_name,
        "calls": calls.calls,
        "covered": calls.calls > 0,
        "codes": _sorted_codes(calls.codes),
        "only_ok": calls.calls > 0 and set(calls.codes) == {"OK"},
        "unexpected_codes": sorted(code for code in calls.codes if code not in expected),
        "contracts": ratio(sum(item["covered"] for item in items), len(items)),
        "fields": fields,
        "enum_values": enums,
        "oneofs": oneofs,
        "expected_codes": codes,
    }


def _server(config: Config, server: ServerConfig, results: Results) -> dict[str, Any]:
    calls = results.servers.get(server.name, {})
    schema = _resolve_schema(config, server, results)
    total_calls = sum(entry.calls for entry in calls.values())
    data: dict[str, Any] = {
        "name": server.name,
        "target": schema.meta.get("target") or server.target,
        "schema": {"source": schema.source, "error": schema.error, **schema.meta},
        "calls": total_calls,
        "called": total_calls > 0,
        "methods": ratio(0, 0),
        "contracts": ratio(0, 0),
        "services": [],
        "out_of_schema": [],
        "excluded_services": schema.excluded,
        "fail_under": server.fail_under,
        "fail_under_contracts": server.fail_under_contracts,
    }
    if schema.error is not None:
        return data

    methods_total = {"covered": 0, "total": 0}
    contracts_total = {"covered": 0, "total": 0}
    known: set[str] = set()
    for service_name in schema.services:
        service = find_service(schema.pool, service_name)
        if service is None:
            continue
        methods = [_method(server, method, calls.get(f"/{service_name}/{method.name}")) for method in service.methods]
        known.update(method["full_name"] for method in methods)
        service_methods = ratio(sum(m["covered"] for m in methods), len(methods))
        service_contracts = ratio(
            sum(m["contracts"]["covered"] for m in methods), sum(m["contracts"]["total"] for m in methods)
        )
        _add(methods_total, service_methods)
        _add(contracts_total, service_contracts)
        data["services"].append(
            {
                "name": service_name,
                "calls": sum(m["calls"] for m in methods),
                "methods": service_methods,
                "contracts": service_contracts,
                "method_list": methods,
            }
        )
    exclude = config.server_exclude(server)
    for full, entry in sorted(calls.items()):
        if full in known:
            continue
        service_name = full.strip("/").split("/", 1)[0]
        if not selected(service_name, server.include, exclude):
            continue  # excluded on purpose (health checks, reflection), not a sign of a stale schema
        data["out_of_schema"].append({"method": full, "calls": entry.calls, "codes": _sorted_codes(entry.codes)})
    data["methods"] = ratio(**methods_total)
    data["contracts"] = ratio(**contracts_total)
    return data


def _below(value: dict[str, Any], threshold: float | None) -> bool:
    if threshold is None or value["total"] == 0:
        return False
    return value["covered"] * 100 < threshold * value["total"]


def compute(config: Config, results: Results, servers: list[str] | None = None) -> dict[str, Any]:
    names = list(config.servers) if not servers else servers
    server_data = [_server(config, config.servers[name], results) for name in names]
    methods = {"covered": 0, "total": 0}
    contracts = {"covered": 0, "total": 0}
    for server in server_data:
        _add(methods, server["methods"])
        _add(contracts, server["contracts"])
    totals = {"methods": ratio(**methods), "contracts": ratio(**contracts)}

    failures: list[str] = []
    for server in server_data:
        if server["schema"]["error"]:
            error = server["schema"]["error"]
            failures.append(f"{server['name']}: no schema ({error['code']}): {error['message']}")
            continue
        method_threshold = server["fail_under"] if server["fail_under"] is not None else config.fail_under
        contract_threshold = (
            server["fail_under_contracts"]
            if server["fail_under_contracts"] is not None
            else config.fail_under_contracts
        )
        if _below(server["methods"], method_threshold):
            failures.append(f"{server['name']}: methods {pct(server['methods'])} < fail-under {method_threshold:g}%")
        if _below(server["contracts"], contract_threshold):
            failures.append(
                f"{server['name']}: contracts {pct(server['contracts'])} < fail-under-contracts {contract_threshold:g}%"
            )
    if _below(totals["methods"], config.fail_under):
        failures.append(f"total: methods {pct(totals['methods'])} < fail-under {config.fail_under:g}%")
    if _below(totals["contracts"], config.fail_under_contracts):
        failures.append(
            f"total: contracts {pct(totals['contracts'])} < fail-under-contracts {config.fail_under_contracts:g}%"
        )

    declared = set(config.servers)
    unknown = [
        {"name": name, "calls": sum(entry.calls for entry in calls.values())}
        for name, calls in sorted(results.servers.items())
        if name not in declared
    ]
    return {
        "format": FORMAT_VERSION,
        "grpc_coverage": __version__,
        "title": config.title,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "calls": sum(server["calls"] for server in server_data),
        "result_files": results.files,
        "totals": totals,
        "thresholds": {"fail_under": config.fail_under, "fail_under_contracts": config.fail_under_contracts},
        "failures": failures,
        "servers": server_data,
        "unknown_servers": unknown,
        "warnings": results.warnings,
    }


def pct(value: dict[str, Any]) -> str:
    """`70%`, `62.2%`, `—` for nothing to cover."""
    if value["percent"] is None:
        return "—"
    text = f"{value['covered'] * 100 / value['total']:.1f}".rstrip("0").rstrip(".")
    return f"{text}%"
