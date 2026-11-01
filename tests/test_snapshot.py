"""Schema snapshots: reflection versions, error markers, `snapshot(channel)` with a runtime token, aio."""

import asyncio
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest
from google.protobuf import descriptor_pb2
from helpers import Stubs, aio_auth_interceptors, auth_interceptor, modules, service, write_project

from grpc_coverage import CoverageObserver, load_config
from grpc_coverage.observer import _writers
from grpc_coverage.snapshot import has_snapshot, marker_path, snapshot_paths, snapshot_target


def project(root, servers_toml, common=""):
    write_project(root, f"[tool.grpc-coverage]\n{common}\n{servers_toml}")
    return load_config(start=root, env={})


def meta(config, server):
    return json.loads(snapshot_paths(config.results_dir, server)[1].read_text())


def marker(config, server, kind="error"):
    return json.loads(marker_path(config.results_dir, server, kind).read_text())


def file_set(config, server):
    return descriptor_pb2.FileDescriptorSet.FromString(snapshot_paths(config.results_dir, server)[0].read_bytes())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _fresh_writers():
    yield
    _writers.clear()


# ---------------------------------------------------------------------------- reflection versions


def test_reflection_v1(tmp_path, servers):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"')
    outcome = snapshot_target(config, config.servers["account"], taken_by="cli")
    assert (outcome.status, outcome.version) == ("taken", "v1")
    data = meta(config, "account")
    assert data["reflection"] == "v1" and data["taken_by"] == "cli" and data["target"] == servers["v1"].target
    assert data["services"] == ["account.v1.Account", "account.v1.Admin", "common.v1.Ping"]
    assert set(data["excluded"]) == {"grpc.reflection.v1alpha.ServerReflection", "grpc.reflection.v1.ServerReflection"}
    names = {f.name for f in file_set(config, "account").file}
    # dependencies come along, including the well-known ones
    assert {"account/v1/account.proto", "common/v1/common.proto", "google/protobuf/timestamp.proto"} <= names


def test_reflection_v1alpha_only(tmp_path, servers):
    config = project(tmp_path, f'[tool.grpc-coverage.billing]\ntarget = "{servers["v1alpha"].target}"')
    outcome = snapshot_target(config, config.servers["billing"], taken_by="plugin")
    assert (outcome.status, outcome.version) == ("taken", "v1alpha")
    assert meta(config, "billing")["services"] == ["billing.v1.Billing", "common.v1.Ping"]


def test_include_and_exclude(tmp_path, servers):
    config = project(
        tmp_path,
        f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"\n'
        'include = ["account.*"]\nexclude = ["account.v1.Admin"]',
    )
    snapshot_target(config, config.servers["account"], taken_by="cli")
    assert meta(config, "account")["services"] == ["account.v1.Account"]


def test_reflection_disabled_writes_marker(tmp_path, servers):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["none"].target}"\nproto = "protos"')
    outcome = snapshot_target(config, config.servers["account"], taken_by="plugin")
    assert outcome.status == "reflection-disabled"
    assert marker(config, "account", "reflection-disabled")["target"] == servers["none"].target
    assert not has_snapshot(config.results_dir, "account")
    assert not marker_path(config.results_dir, "account", "error").exists()


# ---------------------------------------------------------------------------- errors


def test_unavailable(tmp_path):
    config = project(
        tmp_path, f'[tool.grpc-coverage.account]\ntarget = "127.0.0.1:{free_port()}"', "reflection-timeout = 2"
    )
    outcome = snapshot_target(config, config.servers["account"], taken_by="plugin")
    assert (outcome.status, outcome.code) == ("error", "UNAVAILABLE")
    assert marker(config, "account")["code"] == "UNAVAILABLE"


def test_unauthenticated(tmp_path, auth_server):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{auth_server.target}"')
    outcome = snapshot_target(config, config.servers["account"], taken_by="plugin")
    assert (outcome.status, outcome.code) == ("error", "UNAUTHENTICATED")
    assert marker(config, "account") | {"time": None} == {
        "time": None,
        "server": "account",
        "target": auth_server.target,
        "taken_by": "plugin",
        "code": "UNAUTHENTICATED",
        "message": "missing or expired token",
    }


def test_static_metadata_from_toml_is_sent(tmp_path, auth_server, authority):
    token = authority.login()
    config = project(
        tmp_path,
        f'[tool.grpc-coverage.account]\ntarget = "{auth_server.target}"\nmetadata = {{ authorization = "{token}" }}',
    )
    assert snapshot_target(config, config.servers["account"], taken_by="cli").status == "taken"


def test_timeout(tmp_path):
    with socket.socket() as hanging:  # accepts TCP and never speaks HTTP/2
        hanging.bind(("127.0.0.1", 0))
        hanging.listen()
        target = f"127.0.0.1:{hanging.getsockname()[1]}"
        config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{target}"', "reflection-timeout = 0.5")
        outcome = snapshot_target(config, config.servers["account"], taken_by="plugin")
    assert outcome.code in {"DEADLINE_EXCEEDED", "UNAVAILABLE"}
    assert outcome.elapsed < 5


def test_env_error(tmp_path):
    config = project(tmp_path, '[tool.grpc-coverage.account]\ntarget = "${NO_SUCH_GRPC_TARGET}"')
    outcome = snapshot_target(config, config.servers["account"], taken_by="plugin")
    assert (outcome.status, outcome.code) == ("error", "ENV")
    assert "NO_SUCH_GRPC_TARGET is not set" in marker(config, "account")["message"]


# ---------------------------------------------------------------------------- snapshot(channel)


def test_runtime_token_snapshot_through_the_tests_channel(tmp_path, auth_server, authority):
    """The plugin can't log in: by target it gets UNAUTHENTICATED; the tests' own channel then takes it."""
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{auth_server.target}"')
    assert snapshot_target(config, config.servers["account"], taken_by="plugin").code == "UNAUTHENTICATED"
    assert marker_path(config.results_dir, "account", "error").exists()

    account, _, _ = modules()
    coverage = CoverageObserver("account", config=tmp_path)
    with grpc.insecure_channel(auth_server.target) as raw:
        channel = grpc.intercept_channel(raw, auth_interceptor(authority), coverage.interceptor())
        outcome = coverage.snapshot(channel)
        assert outcome.status == "taken"
        Stubs(channel, service(account, "Account")).Get(account.GetRequest(id="1"))
        assert coverage.snapshot(channel).status == "exists"
    assert not marker_path(config.results_dir, "account", "error").exists()
    assert meta(config, "account")["taken_by"] == "code"
    # reflection went through the same interceptor chain but is not a call of the tests
    calls = [
        json.loads(line) for p in (config.results_dir / "calls").glob("*.jsonl") for line in p.read_text().splitlines()
    ]
    assert [c["method"] for c in calls[1:]] == ["/account.v1.Account/Get"]


def test_snapshot_channel_never_raises(tmp_path):
    write_project(tmp_path, "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    coverage = CoverageObserver("account", config=tmp_path)
    with grpc.insecure_channel(f"127.0.0.1:{free_port()}") as channel:
        outcome = coverage.snapshot(channel)
    assert (outcome.status, outcome.code) == ("error", "UNAVAILABLE")


def test_existing_snapshot_is_not_retaken(tmp_path, servers):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"')
    snapshot_target(config, config.servers["account"], taken_by="cli")
    first = meta(config, "account")
    assert snapshot_target(config, config.servers["account"], taken_by="plugin").status == "exists"
    assert meta(config, "account") == first
    forced = snapshot_target(config, config.servers["account"], taken_by="cli", force=True)
    assert forced.status == "taken"


def test_failed_attempt_does_not_overwrite_a_snapshot(tmp_path, servers):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{servers["v1"].target}"')
    snapshot_target(config, config.servers["account"], taken_by="cli")
    coverage = CoverageObserver("account", config=tmp_path)
    with grpc.insecure_channel(f"127.0.0.1:{free_port()}") as channel:
        assert coverage.snapshot(channel).status == "exists"
    assert not marker_path(config.results_dir, "account", "error").exists()


class _CountReflection(grpc.StreamStreamClientInterceptor):
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def intercept_stream_stream(self, continuation, details, request_iterator):
        if "ServerReflection" in details.method:
            with self.lock:
                self.calls += 1
        return continuation(details, request_iterator)


def _snapshot_at_once(root, target, workers):
    write_project(root, "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    counter = _CountReflection()
    start = threading.Barrier(workers)

    def worker():
        coverage = CoverageObserver("account", config=root)
        with grpc.insecure_channel(target) as raw:
            channel = grpc.intercept_channel(raw, counter, coverage.interceptor())
            start.wait()
            return coverage.snapshot(channel).status

    with ThreadPoolExecutor(workers) as pool:
        statuses = sorted(pool.map(lambda _: worker(), range(workers)))
    return statuses, counter.calls


def test_concurrent_snapshots_send_reflection_once(tmp_path, servers):
    """xdist workers calling `snapshot(channel)` at once: one takes it, the others wait and get `exists`.

    The turn is a lock file, so threads of one process race for it exactly like worker processes do.
    """
    _, alone = _snapshot_at_once(tmp_path / "alone", servers["v1"].target, 1)
    statuses, together = _snapshot_at_once(tmp_path / "together", servers["v1"].target, 6)
    assert statuses == ["exists"] * 5 + ["taken"]
    assert together == alone  # the reflection streams of exactly one snapshot
    schema = tmp_path / "together" / "grpc-coverage-results" / "schema"
    assert sorted(p.name for p in schema.iterdir()) == ["account.binpb", "account.json"]


def test_stale_lock_of_a_dead_worker_is_broken(tmp_path, servers):
    write_project(tmp_path, "[tool.grpc-coverage]\nreflection-timeout = 1\n[tool.grpc-coverage.account]\n")
    lock = tmp_path / "grpc-coverage-results" / "schema" / ".account.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("12345")
    old = time.time() - 60
    os.utime(lock, (old, old))
    coverage = CoverageObserver("account", config=tmp_path)
    with grpc.insecure_channel(servers["v1"].target) as channel:
        assert coverage.snapshot(channel).status == "taken"
    assert not lock.exists()


def test_waiting_worker_takes_over_after_a_failed_attempt(tmp_path, servers):
    """The holder fails and releases the lock without a snapshot: the waiting one tries itself."""
    write_project(tmp_path, "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    lock = tmp_path / "grpc-coverage-results" / "schema" / ".account.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("12345")
    threading.Timer(0.2, lock.unlink).start()
    coverage = CoverageObserver("account", config=tmp_path)
    started = time.monotonic()
    with grpc.insecure_channel(servers["v1"].target) as channel:
        assert coverage.snapshot(channel).status == "taken"
    assert time.monotonic() - started >= 0.2


def test_asnapshot_waits_for_the_turn_too(tmp_path, servers):
    write_project(tmp_path, "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    coverage = CoverageObserver("account", config=tmp_path)

    async def main():
        async with grpc.aio.insecure_channel(servers["v1"].target) as channel:
            return sorted(o.status for o in await asyncio.gather(*(coverage.asnapshot(channel) for _ in range(3))))

    assert asyncio.run(main()) == ["exists", "exists", "taken"]


def test_mixed_up_channels_raise_type_error(tmp_path, servers):
    write_project(tmp_path, "[tool.grpc-coverage]\n[tool.grpc-coverage.account]\n")
    coverage = CoverageObserver("account", config=tmp_path)

    async def main():
        async with grpc.aio.insecure_channel(servers["v1"].target) as aio_channel:
            with pytest.raises(TypeError, match="asnapshot"):
                coverage.snapshot(aio_channel)
        with grpc.insecure_channel(servers["v1"].target) as sync_channel:
            with pytest.raises(TypeError, match="snapshot"):
                await coverage.asnapshot(sync_channel)

    asyncio.run(main())


# ---------------------------------------------------------------------------- asnapshot


@pytest.mark.asyncio(loop_scope="session")
async def test_asnapshot_with_async_auth_in_the_session_loop(tmp_path, auth_server, authority):
    config = project(tmp_path, f'[tool.grpc-coverage.account]\ntarget = "{auth_server.target}"')
    coverage = CoverageObserver("account", config=tmp_path)
    account, _, _ = modules()
    interceptors = [*aio_auth_interceptors(authority), *coverage.aio_interceptors()]
    async with grpc.aio.insecure_channel(auth_server.target, interceptors=interceptors) as channel:
        outcome = await coverage.asnapshot(channel)
        assert (outcome.status, outcome.version) == ("taken", "v1")
        await Stubs(channel, service(account, "Account")).Get(account.GetRequest(id="1"))
    assert meta(config, "account")["services"] == [
        "account.v1.Account",
        "account.v1.Admin",
        "auth.v1.Auth",
        "common.v1.Ping",
    ]
    calls = [
        json.loads(line) for p in (config.results_dir / "calls").glob("*.jsonl") for line in p.read_text().splitlines()
    ]
    assert [c["method"] for c in calls[1:]] == ["/account.v1.Account/Get"]


def test_asnapshot_timeout_writes_error(tmp_path):
    with socket.socket() as hanging:
        hanging.bind(("127.0.0.1", 0))
        hanging.listen()
        target = f"127.0.0.1:{hanging.getsockname()[1]}"
        write_project(
            tmp_path,
            f'[tool.grpc-coverage]\nreflection-timeout = 0.3\n[tool.grpc-coverage.account]\ntarget = "{target}"',
        )
        coverage = CoverageObserver("account", config=tmp_path)

        async def main():
            async with grpc.aio.insecure_channel(target) as channel:
                return await coverage.asnapshot(channel)

        outcome = asyncio.run(main())
    assert (outcome.status, outcome.code) == ("error", "DEADLINE_EXCEEDED")
    assert json.loads(marker_path(coverage.results_dir, "account", "error").read_text())["code"] == "DEADLINE_EXCEEDED"


def test_asnapshot_outer_cancellation_propagates(tmp_path):
    with socket.socket() as hanging:
        hanging.bind(("127.0.0.1", 0))
        hanging.listen()
        target = f"127.0.0.1:{hanging.getsockname()[1]}"
        write_project(tmp_path, "[tool.grpc-coverage]\nreflection-timeout = 30\n[tool.grpc-coverage.account]\n")
        coverage = CoverageObserver("account", config=tmp_path)

        async def main():
            async with grpc.aio.insecure_channel(target) as channel:
                task = asyncio.create_task(coverage.asnapshot(channel))
                await asyncio.sleep(0.2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(main())
    assert not marker_path(coverage.results_dir, "account", "error").exists()
