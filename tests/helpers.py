"""Test servers on 127.0.0.1:0, generated test protos, auth with a rotating token, and a pytest-in-subprocess runner.

Imported both by the tests and by the throwaway projects the tests run with `pytest` in a subprocess
(`tests/` is on their PYTHONPATH, the generated modules are in $GC_GEN_DIR).
"""

import asyncio
import importlib
import os
import subprocess
import sys
import textwrap
import threading
import uuid
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc
from google.protobuf import message_factory
from grpc_reflection.v1alpha import reflection, reflection_pb2

TESTS = Path(__file__).resolve().parent
PROTOS = TESTS / "protos"
PROTO_FILES = ["account/v1/account.proto", "billing/v1/billing.proto", "common/v1/common.proto", "auth/v1/auth.proto"]

# ---------------------------------------------------------------------------- generated code


def generate(out: Path) -> Path:
    """Compile the test protos into `out` (messages and `*_pb2_grpc` stubs) and put it on sys.path."""
    from importlib.resources import files

    out.mkdir(parents=True, exist_ok=True)
    if not (out / "auth" / "v1" / "auth_pb2_grpc.py").exists():
        well_known = str(files("grpc_tools") / "_proto")
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTOS}",
            f"-I{well_known}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
        ]
        subprocess.run([*command, *PROTO_FILES], check=True)
    if str(out) not in sys.path:
        sys.path.insert(0, str(out))
    return out


def gen_dir() -> Path:
    return generate(Path(os.environ["GC_GEN_DIR"]))


def modules() -> tuple[Any, Any, Any]:
    gen_dir()
    return (
        importlib.import_module("account.v1.account_pb2"),
        importlib.import_module("billing.v1.billing_pb2"),
        importlib.import_module("common.v1.common_pb2"),
    )


def message_class(descriptor: Any) -> Any:
    return message_factory.GetMessageClass(descriptor)


# ---------------------------------------------------------------------------- client side


class Stubs:
    """Callables `stubs.Create(request)` for every method of a proto service, built from its descriptor."""

    def __init__(self, channel: Any, service: Any):
        for method in service.methods:
            kind = (
                f"{'stream' if method.client_streaming else 'unary'}_{'stream' if method.server_streaming else 'unary'}"
            )
            request, response = message_class(method.input_type), message_class(method.output_type)
            setattr(
                self,
                method.name,
                getattr(channel, kind)(
                    f"/{service.full_name}/{method.name}",
                    request_serializer=request.SerializeToString,
                    response_deserializer=response.FromString,
                ),
            )


def service(module: Any, name: str) -> Any:
    return module.DESCRIPTOR.services_by_name[name]


# ---------------------------------------------------------------------------- server side


def _account_handlers(account: Any) -> dict[str, Any]:
    def create(request, context):
        if not request.user.name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "user.name is required")
        if request.user.name == "taken":
            context.abort(grpc.StatusCode.ALREADY_EXISTS, "exists")
        return account.CreateResponse(id="u-" + request.user.name)

    def get(request, context):
        if request.id == "missing":
            context.abort(grpc.StatusCode.NOT_FOUND, "no such user")
        return account.User(name=request.id)

    def list_(request, context):
        for i in range(request.limit or 2):
            yield account.User(name=f"user-{i}")

    def upload(request_iterator, context):
        return account.UploadResponse(chunks=sum(1 for _ in request_iterator))

    def chat(request_iterator, context):
        for message in request_iterator:
            yield account.ChatMessage(text=message.text.upper())

    def endless(request_iterator, context):  # stays open until the client cancels
        for message in request_iterator:
            yield account.ChatMessage(text=message.text)
        while context.is_active():
            yield account.ChatMessage(text="tick")
            threading.Event().wait(0.01)

    def block(request, context):
        return account.User(name=request.id, status=account.STATUS_BLOCKED)

    return {
        "Create": create,
        "Get": get,
        "List": list_,
        "Upload": upload,
        "Chat": chat,
        "Block": block,
        "_endless": endless,
    }


def _billing_handlers(billing: Any) -> dict[str, Any]:
    def charge(request, context):
        if request.amount.units <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "amount must be positive")
        return billing.ChargeResponse(charge_id="c-1")

    def refund(request, context):
        return billing.ChargeResponse(charge_id=request.charge_id)

    def watch(request, context):
        yield billing.ChargeResponse(charge_id="c-1")

    return {"Charge": charge, "Refund": refund, "Watch": watch}


def _ping(common: Any):
    def ping(request, context):
        return common.PingReply(message=request.message)

    return ping


def _generic_handler(svc: Any, functions: dict[str, Any]) -> grpc.GenericRpcHandler:
    handlers = {}
    for method in svc.methods:
        kind = f"{'stream' if method.client_streaming else 'unary'}_{'stream' if method.server_streaming else 'unary'}"
        fn = functions[method.name]
        if method.name == "Chat" and "_endless" in functions:
            chat, endless = fn, functions["_endless"]

            def fn(request_iterator, context, chat=chat, endless=endless):
                md = dict(context.invocation_metadata())
                return endless(request_iterator, context) if md.get("x-endless") else chat(request_iterator, context)

        handlers[method.name] = getattr(grpc, f"{kind}_rpc_method_handler")(
            fn,
            request_deserializer=message_class(method.input_type).FromString,
            response_serializer=message_class(method.output_type).SerializeToString,
        )
    return grpc.method_handlers_generic_handler(svc.full_name, handlers)


class TokenAuthority:
    """Issues tokens like a login endpoint; only the two newest tokens are valid."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._valid: list[str] = []
        self.logins = 0

    def login(self) -> str:
        with self._lock:
            self.logins += 1
            token = uuid.uuid4().hex
            self._valid = [*self._valid[-1:], token]
            return token

    def valid(self, token: str | None) -> bool:
        with self._lock:
            return token is not None and token in self._valid


class _AuthServerInterceptor(grpc.ServerInterceptor):
    def __init__(self, authority: TokenAuthority):
        self.authority = authority

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        token = dict(handler_call_details.invocation_metadata).get("authorization")
        if handler is None or handler_call_details.method == LOGIN or self.authority.valid(token):
            return handler

        def deny(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or expired token")

        def deny_stream(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or expired token")
            yield  # pragma: no cover

        request = "stream" if handler.request_streaming else "unary"
        kind = f"{request}_{'stream' if handler.response_streaming else 'unary'}"
        return getattr(grpc, f"{kind}_rpc_method_handler")(
            deny_stream if handler.response_streaming else deny,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


REFLECTION_V1 = "grpc.reflection.v1.ServerReflection"
LOGIN = "/auth.v1.Auth/Login"


def _login(authority: TokenAuthority):
    auth = importlib.import_module("auth.v1.auth_pb2")

    def login(request, context):
        return auth.LoginReply(token=authority.login())

    return auth, login


class TestServer:
    """`kind` is "account" or "billing"; `reflection` is "v1" (v1 and v1alpha), "v1alpha" or "none"."""

    __test__ = False

    def __init__(self, kind: str, reflection_mode: str = "v1", authority: TokenAuthority | None = None):
        account, billing, common = modules()
        interceptors = [_AuthServerInterceptor(authority)] if authority else []
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=16), interceptors=interceptors)
        if kind == "account":
            services = [
                (service(account, "Account"), _account_handlers(account)),
                (service(account, "Admin"), _account_handlers(account)),
            ]
        else:
            services = [(service(billing, "Billing"), _billing_handlers(billing))]
        services.append((service(common, "Ping"), {"Ping": _ping(common)}))
        if authority:  # the server that wants a token also hands them out
            auth, login = _login(authority)
            services.append((service(auth, "Auth"), {"Login": login}))
        self.server.add_generic_rpc_handlers([_generic_handler(svc, fns) for svc, fns in services])
        names = [svc.full_name for svc, _ in services]
        if reflection_mode != "none":
            names.append(reflection.SERVICE_NAME)
            reflection.enable_server_reflection(names, self.server)
        if reflection_mode == "v1":
            servicer = reflection.ReflectionServicer([*names, REFLECTION_V1])
            self.server.add_generic_rpc_handlers(
                [
                    grpc.method_handlers_generic_handler(
                        REFLECTION_V1,
                        {
                            "ServerReflectionInfo": grpc.stream_stream_rpc_method_handler(
                                servicer.ServerReflectionInfo,
                                request_deserializer=reflection_pb2.ServerReflectionRequest.FromString,
                                response_serializer=reflection_pb2.ServerReflectionResponse.SerializeToString,
                            )
                        },
                    )
                ]
            )
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.target = f"127.0.0.1:{self.port}"
        self.server.start()

    def stop(self) -> None:
        self.server.stop(None)


# ---------------------------------------------------------------------------- auth on the client


class _SyncAuth(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    """Logs in on the first call and again every `rotate_every` calls: a token the toml could never hold."""

    def __init__(self, authority: TokenAuthority, rotate_every: int = 3):
        self.authority = authority
        self.rotate_every = rotate_every
        self.calls = 0
        self.token: str | None = None

    def _details(self, details):
        if self.token is None or self.calls % self.rotate_every == 0:
            self.token = self.authority.login()
        self.calls += 1
        metadata = [*(details.metadata or ()), ("authorization", self.token)]
        return details._replace(metadata=metadata) if hasattr(details, "_replace") else _Details(details, metadata)

    def intercept_unary_unary(self, continuation, details, request):
        return continuation(self._details(details), request)

    def intercept_unary_stream(self, continuation, details, request):
        return continuation(self._details(details), request)

    def intercept_stream_unary(self, continuation, details, request_iterator):
        return continuation(self._details(details), request_iterator)

    def intercept_stream_stream(self, continuation, details, request_iterator):
        return continuation(self._details(details), request_iterator)


class _Details(grpc.ClientCallDetails):
    def __init__(self, details, metadata):
        self.method = details.method
        self.timeout = details.timeout
        self.metadata = metadata
        self.credentials = details.credentials
        self.wait_for_ready = getattr(details, "wait_for_ready", None)
        self.compression = getattr(details, "compression", None)


def auth_interceptor(authority: TokenAuthority, rotate_every: int = 3) -> _SyncAuth:
    return _SyncAuth(authority, rotate_every)


class _AioAuthBase:
    def __init__(self, state: dict):
        self.state = state

    async def _details(self, details):
        await asyncio.sleep(0)  # e.g. an async OAuth client
        state = self.state
        if state.get("token") is None or state["calls"] % state["rotate_every"] == 0:
            state["token"] = state["authority"].login()
        state["calls"] += 1
        metadata = grpc.aio.Metadata(*(tuple(details.metadata or ())), ("authorization", state["token"]))
        return grpc.aio.ClientCallDetails(
            details.method, details.timeout, metadata, details.credentials, details.wait_for_ready
        )


class _AioUU(_AioAuthBase, grpc.aio.UnaryUnaryClientInterceptor):
    async def intercept_unary_unary(self, continuation, details, request):
        return await continuation(await self._details(details), request)


class _AioUS(_AioAuthBase, grpc.aio.UnaryStreamClientInterceptor):
    async def intercept_unary_stream(self, continuation, details, request):
        return await continuation(await self._details(details), request)


class _AioSU(_AioAuthBase, grpc.aio.StreamUnaryClientInterceptor):
    async def intercept_stream_unary(self, continuation, details, request_iterator):
        return await continuation(await self._details(details), request_iterator)


class _AioSS(_AioAuthBase, grpc.aio.StreamStreamClientInterceptor):
    async def intercept_stream_stream(self, continuation, details, request_iterator):
        return await continuation(await self._details(details), request_iterator)


def aio_auth_interceptors(authority: TokenAuthority, rotate_every: int = 3) -> list:
    state = {"authority": authority, "rotate_every": rotate_every, "calls": 0, "token": None}
    return [_AioUU(state), _AioUS(state), _AioSU(state), _AioSS(state)]


# ---------------------------------------------------------------------------- projects and subprocess pytest


def write_project(root: Path, toml: str, files: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(textwrap.dedent(toml).lstrip())
    for name, content in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip())
    return root


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 120):
    full_env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(gen_dir())]), **(env or {})}
    for key in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_CURRENT_TEST"):
        full_env.pop(key, None)
    return subprocess.run(args, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=timeout, check=False)


def run_pytest(args: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 120):
    return run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd, env, timeout)


def run_cli(args: list[str], cwd: Path, env: dict[str, str] | None = None):
    return run([sys.executable, "-m", "grpc_coverage.cli", *args], cwd, env)
