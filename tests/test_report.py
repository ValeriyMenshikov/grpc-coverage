"""The report: hand-computed numbers, merging results dirs, schema resolution, thresholds, exit codes, the HTML."""

import hashlib
import importlib.util
import json
import re
import shutil

import grpc
import pytest
from google.protobuf import descriptor_pb2
from helpers import PROTOS, Stubs, TestServer, modules, service, write_project

from grpc_coverage import CoverageObserver, load_config
from grpc_coverage.cli import main
from grpc_coverage.observer import _writers
from grpc_coverage.report import build_report
from grpc_coverage.snapshot import snapshot_paths, snapshot_target


@pytest.fixture(autouse=True)
def _fresh_writers():
    yield
    _writers.clear()


def make(root, toml):
    write_project(root, "[tool.grpc-coverage]\n" + toml)
    return load_config(start=root, env={})


def call_billing(root, target):
    _, billing, _ = modules()
    coverage = CoverageObserver("billing", config=root)
    with grpc.insecure_channel(target) as raw:
        stubs = Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(billing, "Billing"))
        stubs.Charge(
            billing.ChargeRequest(
                account_id="a",
                amount=billing.Money(units=5, currency=billing.CURRENCY_USD),
                card=billing.Card(number="4"),
            )
        )
        with pytest.raises(grpc.RpcError):
            stubs.Charge(billing.ChargeRequest(account_id="a"))


def call_ping(root, server, target):
    _, _, common = modules()
    coverage = CoverageObserver(server, config=root)
    with grpc.insecure_channel(target) as raw:
        Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(common, "Ping")).Ping(
            common.PingRequest(message="x")
        )


def stable(data):
    """coverage.json without the parts that change between runs."""
    data = json.loads(json.dumps(data))
    data.pop("generated_at")
    for server in data["servers"]:
        for key in ("taken_at", "sha256", "target"):
            server["schema"].pop(key, None)
        server.pop("target")
    return data


# ---------------------------------------------------------------------------- golden


BILLING = """
[tool.grpc-coverage.billing]
target = "{target}"
include = ["billing.*"]
{server_keys}
[tool.grpc-coverage.billing.expected-codes]
"/billing.v1.Billing/Charge" = ["INVALID_ARGUMENT"]
"""


def test_golden_numbers_without_running_servers(tmp_path):
    """
    billing.v1.Billing, 3 methods; Charge called twice (OK, INVALID_ARGUMENT); Refund and Watch never.

    Charge contract, 14 items:
      fields   account_id amount amount.units amount.currency card card.number wallet comment      6/8
      enums    amount.currency=CURRENCY_USD amount.currency=CURRENCY_EUR                          1/2
      oneofs   method:card method:wallet                                                          1/2
      codes    OK INVALID_ARGUMENT                                                                2/2   → 10/14
    Refund: charge_id + OK → 0/2; Watch: account_id + OK → 0/2.
    Methods 1/3 = 33.33%; contracts 10/18 = 55.56%.
    """
    server = TestServer("billing", "v1alpha")
    try:
        config = make(tmp_path, BILLING.format(server_keys="", target=server.target))
        assert snapshot_target(config, config.servers["billing"], taken_by="cli").status == "taken"
        call_billing(tmp_path, server.target)
    finally:
        server.stop()

    report = build_report(config)
    data = json.loads((config.report_dir / "coverage.json").read_text())
    assert data == json.loads(json.dumps(report.data))
    (billing,) = data["servers"]
    assert data["totals"] == {
        "methods": {"covered": 1, "total": 3, "percent": 33.33},
        "contracts": {"covered": 10, "total": 18, "percent": 55.56},
    }
    assert data["calls"] == 2 and data["failures"] == [] and report.exit_code == 0
    assert billing["schema"]["source"] == "reflection-v1alpha"
    assert billing["excluded_services"] == ["common.v1.Ping", "grpc.reflection.v1alpha.ServerReflection"]
    (svc,) = billing["services"]
    assert [
        (m["name"], m["calls"], m["contracts"]["covered"], m["contracts"]["total"]) for m in svc["method_list"]
    ] == [
        ("Charge", 2, 10, 14),
        ("Refund", 0, 0, 2),
        ("Watch", 0, 0, 2),
    ]
    charge = svc["method_list"][0]
    assert charge["kind"] == "unary_unary" and charge["request_type"] == "billing.v1.ChargeRequest"
    assert charge["codes"] == {"OK": 1, "INVALID_ARGUMENT": 1}
    assert charge["only_ok"] is False and charge["unexpected_codes"] == []
    assert {f["path"]: f["covered"] for f in charge["fields"]} == {
        "account_id": True,
        "amount": True,
        "amount.units": True,
        "amount.currency": True,
        "card": True,
        "card.number": True,
        "wallet": False,
        "comment": False,
    }
    assert charge["enum_values"] == [
        {"value": "amount.currency=CURRENCY_USD", "covered": True},
        {"value": "amount.currency=CURRENCY_EUR", "covered": False},
    ]
    assert charge["oneofs"] == [
        {"branch": "method:card", "covered": True},
        {"branch": "method:wallet", "covered": False},
    ]
    assert charge["expected_codes"] == [
        {"code": "OK", "covered": True},
        {"code": "INVALID_ARGUMENT", "covered": True},
    ]
    assert report.summary.splitlines() == [
        "billing   methods 1/3 (33.3%)   contracts 10/18 (55.6%)",
        "total     methods 1/3 (33.3%)   contracts 10/18 (55.6%)",
    ]


# ---------------------------------------------------------------------------- merging


def test_merge_order_does_not_matter(tmp_path, servers):
    config_a = make(tmp_path / "a", BILLING.format(server_keys="", target=servers["v1alpha"].target))
    make(tmp_path / "b", BILLING.format(server_keys="", target=servers["v1alpha"].target))
    snapshot_target(config_a, config_a.servers["billing"], taken_by="cli")
    call_billing(tmp_path / "a", servers["v1alpha"].target)
    _writers.clear()
    # dir b: the same schema, a different call
    shutil.copytree(
        tmp_path / "a" / "grpc-coverage-results" / "schema", tmp_path / "b" / "grpc-coverage-results" / "schema"
    )
    _, billing, _ = modules()
    coverage = CoverageObserver("billing", config=tmp_path / "b")
    with grpc.insecure_channel(servers["v1alpha"].target) as raw:
        Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(billing, "Billing")).Refund(
            billing.RefundRequest(charge_id="c")
        )

    dirs = [tmp_path / "a" / "grpc-coverage-results", tmp_path / "b" / "grpc-coverage-results"]
    forward = build_report(config_a, dirs, report_dir=tmp_path / "r1").data
    backward = build_report(config_a, dirs[::-1], report_dir=tmp_path / "r2").data
    assert stable(forward) == stable(backward)
    assert forward["totals"]["methods"]["covered"] == 2 and forward["calls"] == 3


def test_different_snapshots_of_one_server_is_an_error(tmp_path, servers):
    config = make(tmp_path / "a", BILLING.format(server_keys="", target=servers["v1alpha"].target))
    snapshot_target(config, config.servers["billing"], taken_by="cli")
    other = tmp_path / "b" / "schema"
    shutil.copytree(config.results_dir / "schema", other)
    # another build of the server: one more message in a file of its schema
    file_set = descriptor_pb2.FileDescriptorSet.FromString((other / "billing.binpb").read_bytes())
    file_set.file[0].message_type.add(name="AddedInTheNextRelease")
    data = file_set.SerializeToString(deterministic=True)
    (other / "billing.binpb").write_bytes(data)
    meta = json.loads((other / "billing.json").read_text())
    (other / "billing.json").write_text(json.dumps({**meta, "sha256": hashlib.sha256(data).hexdigest()}))

    report = build_report(config, [config.results_dir, tmp_path / "b"])
    error = report.data["servers"][0]["schema"]["error"]
    assert error["code"] == "SCHEMA_MISMATCH" and str(tmp_path / "b") in error["message"]
    assert report.exit_code == 1


@pytest.mark.parametrize("damage", ["truncated", "missing", "garbage-with-its-hash"])
def test_damaged_snapshot_is_an_error_of_its_server(tmp_path, servers, damage):
    """A partial copy of a results dir: a clear error of this server, not a traceback and not "take a snapshot"."""
    config = make(tmp_path, BILLING.format(server_keys="", target=servers["v1alpha"].target))
    snapshot_target(config, config.servers["billing"], taken_by="cli")
    binpb, meta_path = snapshot_paths(config.results_dir, "billing")
    if damage == "truncated":
        binpb.write_bytes(binpb.read_bytes()[:-7])
    elif damage == "missing":
        binpb.unlink()
    else:  # bytes that are not a FileDescriptorSet, with a matching hash: only parsing can tell
        garbage = b"\xff\xff\xff\x01garbage"
        binpb.write_bytes(garbage)
        meta = json.loads(meta_path.read_text())
        meta_path.write_text(json.dumps({**meta, "sha256": hashlib.sha256(garbage).hexdigest()}))

    report = build_report(config)
    error = report.data["servers"][0]["schema"]["error"]
    expected = "SCHEMA" if damage == "garbage-with-its-hash" else "SNAPSHOT_INCOMPLETE"
    assert error["code"] == expected, error
    assert report.exit_code == 1
    assert main(["report", "--config", str(tmp_path)]) == 1  # the CLI too: exit code, no traceback


# ---------------------------------------------------------------------------- where the schema comes from


def test_no_schema_fails_with_a_hint(tmp_path, capsys):
    make(tmp_path, "[tool.grpc-coverage.account]\n")
    assert main(["report", "--config", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "account   no schema: NO_SCHEMA" in out
    assert "coverage.snapshot(channel)" in out and "`target`" in out and "`proto`" in out
    assert "FAIL account: no schema (NO_SCHEMA)" in out


ACCOUNT_PROTO = str(PROTOS / "account" / "v1" / "account.proto")
# the CI job without the [proto] extra runs everything else
needs_grpc_tools = pytest.mark.skipif(importlib.util.find_spec("grpc_tools") is None, reason="no grpcio-tools")


def test_snapshot_wins_over_a_wrong_proto(tmp_path, servers):
    config = make(
        tmp_path,
        f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"\nproto = "no/such/protos"\n',
    )
    snapshot_target(config, config.servers["account"], taken_by="cli")
    server = build_report(config).data["servers"][0]
    assert server["schema"]["source"] == "reflection-v1" and server["schema"]["error"] is None


@needs_grpc_tools
def test_reflection_disabled_uses_proto(tmp_path, servers):
    config = make(
        tmp_path,
        f'[tool.grpc-coverage.account]\ntarget = "{servers["none"].target}"\n'
        f'proto = "{ACCOUNT_PROTO}"\nproto-include = ["{PROTOS}"]\n',
    )
    assert snapshot_target(config, config.servers["account"], taken_by="cli").status == "reflection-disabled"
    server = build_report(config).data["servers"][0]
    assert server["schema"]["source"] == "proto"
    # only the services of the given files: common.v1.Ping is not in account.proto
    assert [s["name"] for s in server["services"]] == ["account.v1.Account", "account.v1.Admin"]


def test_reflection_disabled_without_proto_is_a_clear_error(tmp_path, servers):
    config = make(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["none"].target}"\n')
    snapshot_target(config, config.servers["account"], taken_by="cli")
    report = build_report(config)
    error = report.data["servers"][0]["schema"]["error"]
    assert error["code"] == "REFLECTION_DISABLED" and "point `proto`" in error["message"]
    assert report.exit_code == 1


def test_error_marker_does_not_fall_back_to_proto(tmp_path):
    config = make(
        tmp_path,
        f'reflection-timeout = 1\n[tool.grpc-coverage.account]\ntarget = "127.0.0.1:1"\nproto = "{ACCOUNT_PROTO}"\n',
    )
    snapshot_target(config, config.servers["account"], taken_by="plugin")
    error = build_report(config).data["servers"][0]["schema"]["error"]
    assert error["code"] == "UNAVAILABLE" and error["taken_by"] == "plugin"


@needs_grpc_tools
def test_proto_without_any_snapshot_attempt(tmp_path):
    config = make(tmp_path, f'[tool.grpc-coverage.account]\nproto = "{ACCOUNT_PROTO}"\n')
    assert build_report(config).data["servers"][0]["schema"]["source"] == "proto"


def test_proto_without_grpc_tools_says_what_to_install(tmp_path, monkeypatch):
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "grpc_tools" else find_spec(name, *a)
    )
    config = make(tmp_path, f'[tool.grpc-coverage.account]\nproto = "{ACCOUNT_PROTO}"\n')
    report = build_report(config)
    assert "pip install 'grpc-coverage[proto]'" in report.data["servers"][0]["schema"]["error"]["message"]
    assert report.exit_code == 1


# ---------------------------------------------------------------------------- content


def test_calls_outside_the_schema_and_unknown_servers(tmp_path, servers):
    config = make(tmp_path, BILLING.format(server_keys="", target=servers["v1alpha"].target))
    snapshot_target(config, config.servers["billing"], taken_by="cli")
    call_billing(tmp_path, servers["v1alpha"].target)
    calls = next((config.results_dir / "calls").glob("*.jsonl"))
    base = {"kind": "unary_unary", "code": "OK", "request_fields": [], "enum_values": [], "oneofs": []}
    extra = [
        {"server": "billing", "method": "/billing.v1.Billing/Removed", **base},
        {"server": "billing", "method": "/common.v1.Ping/Ping", **base},  # excluded on purpose: not reported
        {"server": "ghost", "method": "/x.v1.X/Y", **base},
    ]
    with calls.open("a") as file:
        file.writelines(json.dumps(row) + "\n" for row in extra)
    data = build_report(config).data
    assert data["servers"][0]["out_of_schema"] == [
        {"method": "/billing.v1.Billing/Removed", "calls": 1, "codes": {"OK": 1}}
    ]
    assert data["unknown_servers"] == [{"name": "ghost", "calls": 1}]


def test_same_service_on_two_servers_is_counted_separately(tmp_path, servers):
    config = make(
        tmp_path,
        f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"\n'
        f'[tool.grpc-coverage.billing]\ntarget = "{servers["v1alpha"].target}"\n',
    )
    for server in config.servers.values():
        snapshot_target(config, server, taken_by="cli")
    call_ping(tmp_path, "account", servers["v1"].target)
    data = build_report(config).data
    ping = {s["name"]: next(x for x in s["services"] if x["name"] == "common.v1.Ping") for s in data["servers"]}
    assert ping["account"]["methods"]["covered"] == 1
    assert ping["billing"]["methods"]["covered"] == 0
    assert [s["called"] for s in data["servers"]] == [True, False]
    assert "not called" in build_report(config).summary.splitlines()[1]


def test_unexpected_codes_and_only_ok(tmp_path, servers):
    config = make(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"\n')
    snapshot_target(config, config.servers["account"], taken_by="cli")
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=tmp_path)
    with grpc.insecure_channel(servers["v1"].target) as raw:
        stubs = Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(account, "Account"))
        stubs.Get(account.GetRequest(id="1"))
        with pytest.raises(grpc.RpcError):
            stubs.Get(account.GetRequest(id="missing"))
        stubs.Create(account.CreateRequest(user=account.User(name="a")))
    methods = {m["name"]: m for m in build_report(config).data["servers"][0]["services"][0]["method_list"]}
    assert methods["Get"]["unexpected_codes"] == ["NOT_FOUND"] and methods["Get"]["only_ok"] is False
    assert methods["Create"]["only_ok"] is True


# ---------------------------------------------------------------------------- thresholds and exit codes


def thresholds_project(tmp_path, servers, toml, server_keys=""):
    config = make(tmp_path, toml.format(target=servers["v1alpha"].target, server_keys=server_keys))
    snapshot_target(config, config.servers["billing"], taken_by="cli")
    call_billing(tmp_path, servers["v1alpha"].target)  # methods 33.33%, contracts 55.56%
    return config


def test_global_threshold(tmp_path, servers):
    config = thresholds_project(tmp_path, servers, "fail-under = 50\n" + BILLING)
    report = build_report(config)
    assert report.data["failures"] == [
        "billing: methods 33.3% < fail-under 50%",
        "total: methods 33.3% < fail-under 50%",
    ]
    assert report.exit_code == 1
    assert "FAIL billing: methods 33.3% < fail-under 50%" in report.summary


def test_server_threshold_overrides_global(tmp_path, servers):
    config = thresholds_project(
        tmp_path, servers, "fail-under = 30\nfail-under-contracts = 50\n" + BILLING, "fail-under = 40"
    )
    assert build_report(config).data["failures"] == ["billing: methods 33.3% < fail-under 40%"]


def test_server_threshold_lower_than_global(tmp_path, servers):
    config = thresholds_project(tmp_path, servers, "fail-under = 90\n" + BILLING, "fail-under = 30")
    # the server passes its own threshold; the totals still answer to the global one
    assert build_report(config).data["failures"] == ["total: methods 33.3% < fail-under 90%"]


def test_exit_codes(tmp_path, servers, capsys):
    thresholds_project(tmp_path, servers, BILLING)
    root = str(tmp_path)
    assert main(["report", "--config", root]) == 0
    assert main(["report", "--config", root, "--fail-under", "50"]) == 1
    assert main(["report", "--config", root, "--fail-under-contracts", "50"]) == 0
    assert main(["report", "--config", root, "--server", "nope"]) == 2
    assert main(["report", "--config", root, "--format", "pdf"]) == 2
    assert main(["report", "--config", str(tmp_path / "missing.toml")]) == 2
    assert "error: --server: unknown server(s) nope" in capsys.readouterr().err

    future = tmp_path / "grpc-coverage-results" / "calls" / "future.jsonl"
    future.write_text(json.dumps({"type": "header", "format": "2.0"}) + "\n")
    assert main(["report", "--config", root]) == 2
    assert "2.0" in capsys.readouterr().err


def test_report_dir_flag_and_formats(tmp_path, servers):
    thresholds_project(tmp_path, servers, BILLING)
    assert (
        main(["report", "--config", str(tmp_path), "--report-dir", str(tmp_path / "only-json"), "--format", "json"])
        == 0
    )
    assert [p.name for p in (tmp_path / "only-json").iterdir()] == ["coverage.json"]


def test_explicit_results_dirs(tmp_path, servers):
    config = thresholds_project(tmp_path, servers, BILLING)
    moved = tmp_path / "ci-artifacts" / "run-1"
    shutil.move(config.results_dir, moved)
    assert main(["report", str(moved), "--config", str(tmp_path)]) == 0
    assert json.loads((config.report_dir / "coverage.json").read_text())["calls"] == 2


# ---------------------------------------------------------------------------- html


def test_html_is_self_contained(tmp_path, servers):
    config = thresholds_project(tmp_path, servers, BILLING)
    build_report(config)
    page = (config.report_dir / "index.html").read_text()
    assert not re.search(r"https?://|//cdn|<link\b|<script[^>]+src=|@import|url\(", page)
    embedded = re.search(r'<script type="application/json" id="grpc-coverage-data">(.*?)</script>', page, re.S)
    assert json.loads(embedded.group(1).replace("<\\/", "</"))["totals"]["methods"]["covered"] == 1
    assert "billing.v1.Billing" in page and "/billing.v1.Billing/Charge" in page


def test_html_escapes_everything(tmp_path):
    config = make(tmp_path, 'title = "<script>alert(1)</script>"\n[tool.grpc-coverage.account]\n')
    build_report(config)
    page = (config.report_dir / "index.html").read_text()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_snapshot_paths_are_read_back(tmp_path, servers):
    config = make(tmp_path, BILLING.format(server_keys="", target=servers["v1alpha"].target))
    snapshot_target(config, config.servers["billing"], taken_by="cli")
    binpb, meta = snapshot_paths(config.results_dir, "billing")
    assert binpb.stat().st_size > 0 and json.loads(meta.read_text())["format"] == "1.0"
