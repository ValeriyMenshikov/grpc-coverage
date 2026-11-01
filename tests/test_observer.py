"""What lands in `calls/*.jsonl` for sync and aio channels and all four call kinds."""

import asyncio
import json

import grpc
import pytest
from helpers import Stubs, modules, service, write_project

from grpc_coverage import CoverageObserver
from grpc_coverage.observer import _writers

TOML = """
[tool.grpc-coverage]
[tool.grpc-coverage.account]
"""


@pytest.fixture
def project(tmp_path):
    write_project(tmp_path, TOML)
    yield tmp_path
    _writers.clear()  # every test gets a fresh calls file


def records(results_dir):
    lines = []
    for path in sorted((results_dir / "calls").glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert rows[0]["type"] == "header"
        lines.extend(rows[1:])
    return lines


def header(results_dir):
    path = next((results_dir / "calls").glob("*.jsonl"))
    return json.loads(path.read_text().splitlines()[0])


def calls_of_all_kinds(stubs, account):
    stubs.Create(account.CreateRequest(user=account.User(name="ann", status=account.STATUS_ACTIVE), email="a@b"))
    list(stubs.List(account.ListRequest(limit=2, statuses=[account.STATUS_BLOCKED])))
    stubs.Upload(iter([account.UploadRequest(chunk=b"1"), account.UploadRequest(tree=account.Node(name="t"))]))
    chat = account.ChatMessage(text="hi")
    chat.addresses["home"].city = "Oslo"
    list(stubs.Chat(iter([chat, account.ChatMessage(text="again")])))
    with pytest.raises(grpc.RpcError):
        stubs.Create(account.CreateRequest())


EXPECTED = [
    {
        "server": "account",
        "method": "/account.v1.Account/Create",
        "kind": "unary_unary",
        "code": "OK",
        "request_fields": ["email", "user", "user.name", "user.status"],
        "enum_values": ["user.status=STATUS_ACTIVE"],
        "oneofs": ["contact:email"],
    },
    {
        "server": "account",
        "method": "/account.v1.Account/List",
        "kind": "unary_stream",
        "code": "OK",
        "request_fields": ["limit", "statuses"],
        "enum_values": ["statuses=STATUS_BLOCKED"],
        "oneofs": [],
    },
    {
        "server": "account",
        "method": "/account.v1.Account/Upload",
        "kind": "stream_unary",
        "code": "OK",
        "request_fields": ["chunk", "tree", "tree.name"],
        "enum_values": [],
        "oneofs": [],
    },
    {
        "server": "account",
        "method": "/account.v1.Account/Chat",
        "kind": "stream_stream",
        "code": "OK",
        "request_fields": ["addresses", "addresses.city", "text"],
        "enum_values": [],
        "oneofs": [],
    },
    {
        "server": "account",
        "method": "/account.v1.Account/Create",
        "kind": "unary_unary",
        "code": "INVALID_ARGUMENT",
        "request_fields": [],
        "enum_values": [],
        "oneofs": [],
    },
]


def test_sync_all_kinds(project, servers):
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=project)
    with grpc.insecure_channel(servers["v1"].target) as raw:
        channel = grpc.intercept_channel(raw, coverage.interceptor())
        calls_of_all_kinds(Stubs(channel, service(account, "Account")), account)
    assert records(coverage.results_dir) == EXPECTED
    head = header(coverage.results_dir)
    assert head["format"] == "1.0" and head["grpc_coverage"] and head["grpc_observer"] and head["grpcio"]


def test_aio_all_kinds(project, servers):
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=project)

    async def main():
        async with grpc.aio.insecure_channel(servers["v1"].target, interceptors=coverage.aio_interceptors()) as ch:
            stubs = Stubs(ch, service(account, "Account"))
            await stubs.Create(
                account.CreateRequest(user=account.User(name="ann", status=account.STATUS_ACTIVE), email="a@b")
            )
            [_ async for _ in stubs.List(account.ListRequest(limit=2, statuses=[account.STATUS_BLOCKED]))]
            await stubs.Upload(
                iter([account.UploadRequest(chunk=b"1"), account.UploadRequest(tree=account.Node(name="t"))])
            )
            chat = account.ChatMessage(text="hi")
            chat.addresses["home"].city = "Oslo"
            [_ async for _ in stubs.Chat(iter([chat, account.ChatMessage(text="again")]))]
            with pytest.raises(grpc.RpcError):
                await stubs.Create(account.CreateRequest())

    asyncio.run(main())
    assert records(coverage.results_dir) == EXPECTED


def test_aio_break_and_cancel_give_one_cancelled_line_each(project, servers):
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=project)

    async def main():
        async with grpc.aio.insecure_channel(servers["v1"].target, interceptors=coverage.aio_interceptors()) as ch:
            stubs = Stubs(ch, service(account, "Account"))
            call = stubs.Chat(iter([account.ChatMessage(text="x")]), metadata=(("x-endless", "1"),))
            async for _ in call:
                break
            call.cancel()

            async def consume():
                async for _ in stubs.Chat(iter([account.ChatMessage(text="y")]), metadata=(("x-endless", "1"),)):
                    pass

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.1)

    asyncio.run(main())
    rows = records(coverage.results_dir)
    assert [(r["method"], r["code"]) for r in rows] == [("/account.v1.Account/Chat", "CANCELLED")] * 2
    assert rows[0]["request_fields"] == ["text"]


def test_several_channels_one_file_per_process(project, servers):
    account, _, _ = modules()
    a = CoverageObserver("account", config=project)
    b = CoverageObserver("account", config=project)
    for observer in (a, b):
        with grpc.insecure_channel(servers["v1"].target) as raw:
            Stubs(grpc.intercept_channel(raw, observer.interceptor()), service(account, "Account")).Get(
                account.GetRequest(id="1")
            )
    assert len(list((a.results_dir / "calls").glob("*.jsonl"))) == 1
    assert len(records(a.results_dir)) == 2


def test_os_chdir_in_a_test_does_not_move_results(project, servers, tmp_path_factory, monkeypatch):
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=project)
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    with grpc.insecure_channel(servers["v1"].target) as raw:
        Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(account, "Account")).Get(
            account.GetRequest(id="1")
        )
    assert len(records(project / "grpc-coverage-results")) == 1


def test_observer_never_breaks_calls(project, servers, monkeypatch):
    account, _, _ = modules()
    coverage = CoverageObserver("account", config=project)

    def broken(record):
        raise OSError("disk full")

    monkeypatch.setattr(coverage._writer, "write", broken)
    with grpc.insecure_channel(servers["v1"].target) as raw:
        stubs = Stubs(grpc.intercept_channel(raw, coverage.interceptor()), service(account, "Account"))
        assert stubs.Get(account.GetRequest(id="1")).name == "1"
