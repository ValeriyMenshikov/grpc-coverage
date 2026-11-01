import os
from pathlib import Path

import pytest
from helpers import TestServer, TokenAuthority, generate


@pytest.fixture(scope="session", autouse=True)
def gen(tmp_path_factory):
    """Generated modules of tests/protos, shared with the subprocess projects through $GC_GEN_DIR."""
    folder = os.environ.get("GC_GEN_DIR") or str(tmp_path_factory.getbasetemp().parent / "grpc-coverage-gen")
    os.environ["GC_GEN_DIR"] = folder
    return generate(Path(folder))


@pytest.fixture(scope="session")
def servers(gen):
    """Three servers at once: reflection v1 (account), v1alpha only (billing), no reflection (account)."""
    started = {
        "v1": TestServer("account", "v1"),
        "v1alpha": TestServer("billing", "v1alpha"),
        "none": TestServer("account", "none"),
    }
    yield started
    for server in started.values():
        server.stop()


@pytest.fixture(scope="session")
def authority():
    return TokenAuthority()


@pytest.fixture(scope="session")
def auth_server(gen, authority):
    server = TestServer("account", "v1", authority=authority)
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _isolated_session_state():
    """Tests create observers outside a plugin-driven session; don't let one test's plugin path leak."""
    from grpc_coverage import _session

    saved = _session.plugin_config
    _session.plugin_config = None
    yield
    _session.plugin_config = saved
