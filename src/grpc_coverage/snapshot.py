"""Schema snapshots: a reflection client (sync and aio), `snapshot(channel)`, snapshots by `target`, error markers.

    results-dir/schema/<server>.binpb                FileDescriptorSet
    results-dir/schema/<server>.json                 who took it, when, reflection version, hash, services
    results-dir/schema/<server>.reflection-disabled  the server answered UNIMPLEMENTED to both reflection versions
    results-dir/schema/<server>.error                any other failure: code and message

A snapshot is taken once per server per session: an existing one is never retaken (except `--force`). xdist workers
that call `snapshot(channel)` at the same time take turns through a lock file (`schema/.<server>.lock`), so only the
first one sends reflection requests. Every file is written atomically (tmp file + rename), so even without the lock
(a stale one is broken after `reflection-timeout`) the files can't be half-written.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from grpc_coverage._version import FORMAT_VERSION, __version__
from grpc_coverage.config import Config, ServerConfig
from grpc_coverage.schema import descriptor_set, selected

logger = logging.getLogger("grpc_coverage")

# v1 and v1alpha share the wire format, so the v1alpha messages from grpcio-reflection serve both.
VERSIONS = (
    ("v1", "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"),
    ("v1alpha", "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo"),
)

# Set while a snapshot runs. The observer reads it in `on_call_start` (which runs in the caller's context in sync and
# aio channels alike) and drops reflection calls from the coverage.
IN_SNAPSHOT: contextvars.ContextVar[bool] = contextvars.ContextVar("grpc_coverage_in_snapshot", default=False)

_CODE_NAMES = {code.value[0]: code.name for code in grpc.StatusCode}

Request = reflection_pb2.ServerReflectionRequest
Response = reflection_pb2.ServerReflectionResponse


class ReflectionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ReflectionDisabled(Exception):
    """UNIMPLEMENTED for both reflection versions."""


@dataclass
class Reflected:
    version: str
    file_set: descriptor_pb2.FileDescriptorSet
    services: list[str]
    excluded: list[str]


@dataclass
class Outcome:
    """What happened to one server: `taken`, `exists`, `reflection-disabled` or `error`."""

    server: str
    status: str
    message: str = ""
    code: str = ""
    version: str = ""
    elapsed: float = 0.0
    services: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- files


def schema_dir(results_dir: Path) -> Path:
    return results_dir / "schema"


def snapshot_paths(results_dir: Path, server: str) -> tuple[Path, Path]:
    folder = schema_dir(results_dir)
    return folder / f"{server}.binpb", folder / f"{server}.json"


def marker_path(results_dir: Path, server: str, kind: str) -> Path:
    return schema_dir(results_dir) / f"{server}.{kind}"


def has_snapshot(results_dir: Path, server: str) -> bool:
    return all(path.is_file() for path in snapshot_paths(results_dir, server))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # only left if writing failed, e.g. the disk is full


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_snapshot(
    results_dir: Path, server: str, reflected: Reflected, *, target: str | None, taken_by: str
) -> dict[str, Any]:
    data = reflected.file_set.SerializeToString(deterministic=True)
    meta = {
        "format": FORMAT_VERSION,
        "grpc_coverage": __version__,
        "server": server,
        "target": target,
        "taken_by": taken_by,
        "taken_at": _now(),
        "reflection": reflected.version,
        "sha256": hashlib.sha256(data).hexdigest(),
        "services": reflected.services,
        "excluded": reflected.excluded,
    }
    binpb, meta_path = snapshot_paths(results_dir, server)
    _atomic_write(binpb, data)
    _atomic_write(meta_path, json.dumps(meta, indent=2).encode())
    # A successful snapshot is final: drop what an earlier attempt (e.g. the plugin without a token) left behind.
    for kind in ("error", "reflection-disabled"):
        marker_path(results_dir, server, kind).unlink(missing_ok=True)
    return meta


def write_marker(results_dir: Path, server: str, kind: str, payload: dict[str, Any]) -> None:
    if has_snapshot(results_dir, server):  # someone else succeeded meanwhile: the snapshot wins
        return
    path = marker_path(results_dir, server, kind)
    _atomic_write(path, json.dumps({"time": _now(), **payload}, indent=2).encode())
    if has_snapshot(results_dir, server):  # ...or succeeded between the check and the write
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------- one snapshot for all xdist workers

_LOCK_POLL = 0.02


class _Turn:
    """A lock file for one server's snapshot. `try_take()`: True if this process may take the snapshot now.

    The lock is advisory and never blocks forever: a lock older than `reflection-timeout` (+ a margin for
    interceptors such as a login) belongs to a process that died mid-snapshot and is broken. If the lock file can't
    be created at all (a read-only dir), the snapshot is taken without it.
    """

    def __init__(self, config: Config, server: ServerConfig):
        self.path = schema_dir(config.results_dir) / f".{server.name}.lock"
        self.stale = config.reflection_timeout + 10
        self.held = False

    def try_take(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - self.path.stat().st_mtime > self.stale:
                    self.path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        except OSError:
            return True
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self.held = True
        return True

    def release(self) -> None:
        if self.held:
            self.held = False
            self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------- reflection protocol

# The protocol is a generator: it yields a batch of requests, gets the list of responses back and finally returns
# the result. The sync and the aio drivers below only move batches over one bidi stream each.
Protocol = Generator[list[Any], list[Any], tuple[list[str], list[str], dict[str, descriptor_pb2.FileDescriptorProto]]]


def _checked(response: Any) -> Any:
    if response.HasField("error_response"):
        error = response.error_response
        raise ReflectionError(_CODE_NAMES.get(error.error_code, str(error.error_code)), error.error_message)
    return response


def _protocol(include: Iterable[str], exclude: Iterable[str]) -> Protocol:
    """ListServices → FileContainingSymbol for every selected service → FileByFilename for missing dependencies."""
    responses = yield [Request(list_services="*")]
    if len(responses) != 1:
        raise ReflectionError("INTERNAL", f"expected one response to ListServices, got {len(responses)}")
    names = [service.name for service in _checked(responses[0]).list_services_response.service]
    chosen = [name for name in names if selected(name, include, exclude)]
    files: dict[str, descriptor_pb2.FileDescriptorProto] = {}

    def add(batch: list[Any]) -> None:
        for response in batch:
            for raw in _checked(response).file_descriptor_response.file_descriptor_proto:
                file = descriptor_pb2.FileDescriptorProto.FromString(raw)
                files.setdefault(file.name, file)

    if chosen:
        add((yield [Request(file_containing_symbol=name) for name in chosen]))
    missing: list[str] = []
    while True:
        previous, missing = missing, sorted({dep for file in files.values() for dep in file.dependency} - set(files))
        if not missing:
            return names, chosen, files
        if missing == previous:
            raise ReflectionError("NOT_FOUND", f"the server did not return the files {', '.join(missing)}")
        add((yield [Request(file_by_filename=name) for name in missing]))


def _result(version: str, value: tuple[list[str], list[str], dict]) -> Reflected:
    names, chosen, files = value
    return Reflected(
        version=version,
        file_set=descriptor_set(files.values()),
        services=chosen,
        excluded=[name for name in names if name not in chosen],
    )


def _stub(channel: Any, path: str) -> Any:
    return channel.stream_stream(
        path, request_serializer=Request.SerializeToString, response_deserializer=Response.FromString
    )


def reflect(
    channel: grpc.Channel,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    timeout: float = 10.0,
    metadata: Mapping[str, str] | None = None,
) -> Reflected:
    """Read the schema of the server behind a sync channel. Raises `ReflectionDisabled` / `ReflectionError`."""
    deadline = time.monotonic() + timeout
    md = tuple((metadata or {}).items()) or None
    include, exclude = list(include), list(exclude)
    for version, path in VERSIONS:
        stub = _stub(channel, path)
        protocol = _protocol(include, exclude)
        try:
            batch = next(protocol)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReflectionError("DEADLINE_EXCEEDED", f"reflection did not finish in {timeout:g}s")
                batch = protocol.send(list(stub(iter(batch), timeout=remaining, metadata=md)))
        except StopIteration as stop:
            return _result(version, stop.value)
        except grpc.RpcError as error:
            code = error.code() if callable(getattr(error, "code", None)) else None
            if code == grpc.StatusCode.UNIMPLEMENTED:
                continue
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise ReflectionError("DEADLINE_EXCEEDED", f"reflection did not finish in {timeout:g}s") from error
            raise ReflectionError(code.name if code else "UNKNOWN", _details(error)) from error
    raise ReflectionDisabled()


async def areflect(
    channel: grpc.aio.Channel,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    timeout: float = 10.0,
    metadata: Mapping[str, str] | None = None,
) -> Reflected:
    """`reflect` for a grpc.aio channel. The timeout is `asyncio.timeout`; outer cancellation propagates."""
    md = tuple((metadata or {}).items()) or None
    include, exclude = list(include), list(exclude)
    try:
        async with asyncio.timeout(timeout):
            for version, path in VERSIONS:
                stub = _stub(channel, path)
                protocol = _protocol(include, exclude)
                try:
                    batch = next(protocol)
                    while True:
                        batch = protocol.send([response async for response in stub(iter(batch), metadata=md)])
                except StopIteration as stop:
                    return _result(version, stop.value)
                except grpc.RpcError as error:
                    code = error.code() if callable(getattr(error, "code", None)) else None
                    if code == grpc.StatusCode.UNIMPLEMENTED:
                        continue
                    raise ReflectionError(code.name if code else "UNKNOWN", _details(error)) from error
    except TimeoutError as error:
        raise ReflectionError("DEADLINE_EXCEEDED", f"reflection did not finish in {timeout:g}s") from error
    raise ReflectionDisabled()


def _details(error: grpc.RpcError) -> str:
    details = getattr(error, "details", None)
    return (details() if callable(details) else None) or str(error)


# ---------------------------------------------------------------------------- snapshots


def _record_failure(
    config: Config, server: ServerConfig, exc: BaseException, *, taken_by: str, target: str | None, started: float
) -> Outcome:
    elapsed = time.monotonic() - started
    if isinstance(exc, ReflectionDisabled):
        write_marker(
            config.results_dir,
            server.name,
            "reflection-disabled",
            {"server": server.name, "target": target, "taken_by": taken_by},
        )
        return Outcome(server.name, "reflection-disabled", "reflection is disabled on the server", elapsed=elapsed)
    if isinstance(exc, ReflectionError):
        code, message = exc.code, exc.message
    else:
        code, message = type(exc).__name__, str(exc) or repr(exc)
    write_marker(
        config.results_dir,
        server.name,
        "error",
        {"server": server.name, "target": target, "taken_by": taken_by, "code": code, "message": message},
    )
    return Outcome(server.name, "error", message, code=code, elapsed=elapsed)


def _taken(
    config: Config, server: ServerConfig, reflected: Reflected, *, taken_by: str, target: str | None, started: float
) -> Outcome:
    write_snapshot(config.results_dir, server.name, reflected, target=target, taken_by=taken_by)
    return Outcome(
        server.name,
        "taken",
        version=reflected.version,
        elapsed=time.monotonic() - started,
        services=reflected.services,
    )


def _log(outcome: Outcome, how: str) -> None:
    if outcome.status == "error":
        logger.warning(
            "grpc-coverage: %s: schema snapshot %s failed: %s %s", outcome.server, how, outcome.code, outcome.message
        )
    elif outcome.status == "reflection-disabled":
        logger.warning("grpc-coverage: %s: reflection is disabled, the report will use `proto`", outcome.server)


def snapshot_channel(config: Config, server: ServerConfig, channel: Any) -> Outcome:
    """`coverage.snapshot(channel)`: reflection through the tests' own channel. Never raises."""
    turn = _Turn(config, server)
    while True:  # another xdist worker may be taking it right now: wait for its result
        if has_snapshot(config.results_dir, server.name):
            return Outcome(server.name, "exists")
        if turn.try_take():
            break
        time.sleep(_LOCK_POLL)
    try:
        return _snapshot_channel(config, server, channel)
    finally:
        turn.release()


def _snapshot_channel(config: Config, server: ServerConfig, channel: Any) -> Outcome:
    if has_snapshot(config.results_dir, server.name):  # taken between the check and the lock
        return Outcome(server.name, "exists")
    started = time.monotonic()
    token = IN_SNAPSHOT.set(True)
    try:
        reflected = reflect(
            channel,
            include=server.include,
            exclude=config.server_exclude(server),
            timeout=config.reflection_timeout,
        )
        outcome = _taken(config, server, reflected, taken_by="code", target=server.target, started=started)
    except Exception as exc:
        outcome = _record_failure(config, server, exc, taken_by="code", target=server.target, started=started)
    finally:
        IN_SNAPSHOT.reset(token)
    _log(outcome, "through the channel")
    return outcome


async def asnapshot_channel(config: Config, server: ServerConfig, channel: Any) -> Outcome:
    """`await coverage.asnapshot(channel)`. Never raises, except `CancelledError` from outside."""
    turn = _Turn(config, server)
    while True:
        if has_snapshot(config.results_dir, server.name):
            return Outcome(server.name, "exists")
        if turn.try_take():
            break
        await asyncio.sleep(_LOCK_POLL)
    try:
        return await _asnapshot_channel(config, server, channel)
    finally:
        turn.release()


async def _asnapshot_channel(config: Config, server: ServerConfig, channel: Any) -> Outcome:
    if has_snapshot(config.results_dir, server.name):
        return Outcome(server.name, "exists")
    started = time.monotonic()
    token = IN_SNAPSHOT.set(True)
    try:
        reflected = await areflect(
            channel,
            include=server.include,
            exclude=config.server_exclude(server),
            timeout=config.reflection_timeout,
        )
        outcome = _taken(config, server, reflected, taken_by="code", target=server.target, started=started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = _record_failure(config, server, exc, taken_by="code", target=server.target, started=started)
    finally:
        IN_SNAPSHOT.reset(token)
    _log(outcome, "through the channel")
    return outcome


def target_channel(server: ServerConfig) -> grpc.Channel:
    assert server.target is not None
    if not server.tls:
        return grpc.insecure_channel(server.target)
    root = server.root_cert.read_bytes() if server.root_cert else None
    return grpc.secure_channel(server.target, grpc.ssl_channel_credentials(root_certificates=root))


def snapshot_target(config: Config, server: ServerConfig, *, taken_by: str, force: bool = False) -> Outcome:
    """Snapshot by `target` from the toml (the plugin at session start, `grpc-coverage snapshot`). Never raises."""
    if not force and has_snapshot(config.results_dir, server.name):
        return Outcome(server.name, "exists")
    if force:
        for path in snapshot_paths(config.results_dir, server.name):
            path.unlink(missing_ok=True)
    started = time.monotonic()
    try:
        if server.snapshot_error:
            raise ReflectionError("ENV", server.snapshot_error)
        with target_channel(server) as channel:
            reflected = reflect(
                channel,
                include=server.include,
                exclude=config.server_exclude(server),
                timeout=config.reflection_timeout,
                metadata=server.metadata,
            )
        outcome = _taken(config, server, reflected, taken_by=taken_by, target=server.target, started=started)
    except Exception as exc:
        outcome = _record_failure(config, server, exc, taken_by=taken_by, target=server.target, started=started)
    return outcome


def snapshot_targets(
    config: Config, servers: Iterable[ServerConfig], *, taken_by: str, force: bool = False
) -> list[Outcome]:
    """Snapshot several servers by `target` in parallel, each with its own `reflection-timeout`."""
    from concurrent.futures import ThreadPoolExecutor

    servers = list(servers)
    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(servers)), thread_name_prefix="grpc-coverage") as pool:
        return list(pool.map(lambda s: snapshot_target(config, s, taken_by=taken_by, force=force), servers))
