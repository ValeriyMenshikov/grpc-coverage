"""`CoverageObserver`: one JSON line per call into `results-dir/calls/<pid>-<uuid>.jsonl`.

Hooks run inline (in aio channels — right in the event loop), so the only I/O is appending a line to a file that is
already open: no fsync, a `threading.Lock` around the write only (sync request streams are read in a gRPC worker
thread), no `await`. Each process has its own file, so xdist workers never share one.
"""

import inspect
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import IO, Any

import grpc
from google.protobuf.message import Message
from grpc_observer import GrpcCall, GrpcObserver, GrpcResult

from grpc_coverage import _session
from grpc_coverage._version import FORMAT_VERSION, __version__
from grpc_coverage.config import Config, load_config
from grpc_coverage.paths import Collected, collect
from grpc_coverage.snapshot import IN_SNAPSHOT, Outcome, asnapshot_channel, snapshot_channel

logger = logging.getLogger("grpc_coverage")

_SKIP = "grpc_coverage.skip"
_COLLECTED = "grpc_coverage.collected"
_PACKAGE_DIR = Path(__file__).resolve().parent


def header() -> dict[str, Any]:
    import grpc_observer

    return {
        "type": "header",
        "format": FORMAT_VERSION,
        "grpc_coverage": __version__,
        "grpc_observer": getattr(grpc_observer, "__version__", "?"),
        "grpcio": grpc.__version__,
        "pid": os.getpid(),
    }


class _Writer:
    """The calls file of this process for one results dir. Opened on the first call, reopened after a fork."""

    def __init__(self, results_dir: Path):
        self.results_dir = results_dir
        self._lock = threading.Lock()
        self._file: IO[str] | None = None
        self._pid = 0

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            if self._file is None or self._pid != os.getpid():
                folder = self.results_dir / "calls"
                folder.mkdir(parents=True, exist_ok=True)
                self._pid = os.getpid()
                self._file = open(folder / f"{self._pid}-{uuid.uuid4().hex}.jsonl", "a", encoding="utf-8")  # noqa: SIM115
                self._file.write(json.dumps(header(), separators=(",", ":")) + "\n")
            self._file.write(line)
            self._file.flush()


_writers: dict[Path, _Writer] = {}
_writers_lock = threading.Lock()


def _writer(results_dir: Path) -> _Writer:
    with _writers_lock:
        writer = _writers.get(results_dir)
        if writer is None:
            writer = _writers[results_dir] = _Writer(results_dir)
        return writer


def _caller_dir() -> Path:
    """Folder of the first frame outside this package: the file that created the observer (usually conftest.py)."""
    frame = inspect.currentframe()
    try:
        while frame is not None:
            # `__file__` of the module is absolute since Python 3.9; `co_filename` may be relative to the cwd at
            # import time (a relative sys.path entry), and the cwd may have changed since.
            filename = frame.f_globals.get("__file__") or frame.f_code.co_filename
            if filename and not filename.startswith("<"):
                path = Path(filename).resolve()
                if not path.is_relative_to(_PACKAGE_DIR):
                    return path.parent
            frame = frame.f_back
    finally:
        del frame
    return Path.cwd()  # pragma: no cover - only from an interactive prompt


class CoverageObserver(GrpcObserver):
    """Records what tests send to one server declared as `[tool.grpc-coverage.<server>]`.

    sync:  grpc.intercept_channel(channel, CoverageObserver("account").interceptor())
    aio:   grpc.aio.insecure_channel(target, interceptors=CoverageObserver("account").aio_interceptors())

    The config is the nearest `pyproject.toml` with `[tool.grpc-coverage]` above the file that creates the observer,
    or `config=` (a `pyproject.toml` or its folder). The current directory is never used.
    """

    def __init__(self, server: str, *, config: str | os.PathLike[str] | None = None):
        super().__init__()
        start = _caller_dir()
        self.config: Config = load_config(config, start=start)
        if server not in self.config.servers:
            declared = ", ".join(self.config.servers) or "none"
            where = self.config.pyproject or f"{start} (no pyproject.toml with [tool.grpc-coverage] found)"
            raise ValueError(
                f"grpc-coverage: unknown server {server!r}; declared in {where}: {declared}. "
                f"Add a table [tool.grpc-coverage.{server}]"
            )
        self.server = server
        self.results_dir = self.config.results_dir
        self._writer = _writer(self.results_dir)
        _session.register_observer(server, self.config.pyproject)

    # -- wiring: the server name goes to grpc-observer as `target` and comes back as `call.target`

    def interceptor(self, target: str | None = None) -> Any:
        return super().interceptor(target=target or self.server)

    def aio_interceptors(self, target: str | None = None) -> list[grpc.aio.ClientInterceptor]:
        return super().aio_interceptors(target=target or self.server)

    # -- schema

    def snapshot(self, channel: grpc.Channel) -> Outcome:
        """Take the schema of the server through `channel`, with its interceptors (auth). Never raises.

        Pass the very channel the tests use. Once per server per session; reflection calls are not recorded.
        """
        if isinstance(channel, grpc.aio.Channel):
            raise TypeError(
                "snapshot() takes a sync channel; for a grpc.aio channel use `await coverage.asnapshot(channel)`"
            )
        return snapshot_channel(self.config, self.config.servers[self.server], channel)

    async def asnapshot(self, channel: grpc.aio.Channel) -> Outcome:
        """`snapshot` for a grpc.aio channel. Call it in the event loop of the channel."""
        if not isinstance(channel, grpc.aio.Channel):
            raise TypeError("asnapshot() takes a grpc.aio channel; for a sync channel use `coverage.snapshot(channel)`")
        return await asnapshot_channel(self.config, self.config.servers[self.server], channel)

    # -- hooks

    def on_call_start(self, call: GrpcCall) -> None:
        # The flag is read here, in the caller's context; `on_call_end` may run in a done callback of aio.
        if IN_SNAPSHOT.get():
            call.state[_SKIP] = True
        else:
            call.state[_COLLECTED] = Collected()

    def on_request(self, call: GrpcCall, request: Message) -> None:
        collected = call.state.get(_COLLECTED)
        if collected is not None:
            collect(request, collected)

    def on_request_message(self, call: GrpcCall, message: Message, index: int) -> None:
        collected = call.state.get(_COLLECTED)
        if collected is not None:
            collect(message, collected)

    def on_call_end(self, call: GrpcCall, result: GrpcResult) -> None:
        if call.state.get(_SKIP):
            return
        collected = call.state.get(_COLLECTED) or Collected()
        self._writer.write(
            {
                "server": call.target or self.server,
                "method": call.method,
                "kind": call.kind.value,
                "code": result.code.name,
                "request_fields": sorted(collected.request_fields),
                "enum_values": sorted(collected.enum_values),
                "oneofs": sorted(collected.oneofs),
            }
        )
