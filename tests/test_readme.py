"""Every example of the README, executed against local servers.

The README marks its examples with HTML comments right before the code block:

- `<!-- file: <project>/<path> -->` — the block is written to that file of a throwaway project;
- `<!-- run: <project> -->` — each line is run in the project: `pytest`, `grpc-coverage`, `VAR=x` prefixes;
- `<!-- ci: <project> -->` — the `run:` steps of a CI snippet, run the same way;
- `<!-- output: <project> -->` — every line of the block appears in the output of the previous run.

Blocks of one project run in README order. TOML blocks without a marker are fragments: each must load as a config.
"""

import importlib.util
import re
import shlex
from pathlib import Path

import pytest
from helpers import TestServer, TokenAuthority, run_cli, run_pytest

from grpc_coverage.config import load_config

README = Path(__file__).resolve().parents[1] / "README.md"
BLOCK = re.compile(r"(?:<!-- (file|run|ci|output): (\S+) -->\n)?```(\w*)\n(.*?)```", re.DOTALL)
HAS_XDIST = importlib.util.find_spec("xdist") is not None


def blocks():
    return [m.groups() for m in BLOCK.finditer(README.read_text())]


def project_blocks(name):
    return [(kind, target, body) for kind, target, _, body in blocks() if kind and target.split("/")[0] == name]


def servers_for(name):
    if name == "token":
        return {"ACCOUNT_TARGET": TestServer("account", "v1", authority=TokenAuthority())}
    servers = {"ACCOUNT_TARGET": TestServer("account", "v1")}
    if name == "quickstart":
        servers["BILLING_TARGET"] = TestServer("billing", "v1")
    return servers


def run_line(line, root, env):
    words = shlex.split(line)
    extra = {}
    while "=" in words[0]:
        key, value = words.pop(0).split("=", 1)
        extra[key] = value
    if not HAS_XDIST and "-n" in words:  # the CI job without pytest-xdist
        at = words.index("-n")
        del words[at : at + 2]
    program, args = words[0], words[1:]
    runner = {"pytest": run_pytest, "grpc-coverage": run_cli}[program]
    result = runner(args, root, {**env, **extra})
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"$ {line}\n{out}"
    return out


def run_block(kind, body, root, env):
    if kind == "ci":
        lines = [m.group(1) for m in re.finditer(r"- run: (.+)", body)]
    else:
        lines = [line for line in body.splitlines() if line.strip()]
    assert lines
    return "".join(run_line(line, root, env) for line in lines)


@pytest.mark.parametrize("name", ["quickstart", "token", "aio"])
def test_readme_project(tmp_path, gen, name):
    if name == "aio":
        pytest.importorskip("pytest_asyncio")
    items = project_blocks(name)
    assert any(kind == "run" for kind, _, _ in items)
    servers = servers_for(name)
    env = {key: server.target for key, server in servers.items()}
    root = tmp_path / name
    try:
        output = None
        for kind, target, body in items:
            if kind == "file":
                path = tmp_path / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            elif kind in ("run", "ci"):
                output = run_block(kind, body, root, env)
            else:
                assert output is not None, "an output block before any run"
                missing = [line for line in body.splitlines() if line not in output.splitlines()]
                assert not missing, f"{missing} not in\n{output}"
        if name == "token":
            assert "WARNING account: UNAUTHENTICATED" in output or "deferred" in output
            assert not (root / "grpc-coverage-results" / "schema" / "account.error").exists()
        if name == "quickstart":
            check_report_on_finish(root, env)
    finally:
        for server in servers.values():
            server.stop()


def toml_fragments():
    found = [body for kind, _, lang, body in blocks() if lang == "toml" and not kind]
    assert len(found) >= 3
    return found


def check_report_on_finish(root, env):
    fragment = next(body for body in toml_fragments() if "report-on-finish" in body)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("[tool.grpc-coverage]\n", fragment, 1))
    run_line("grpc-coverage clean", root, env)
    out = run_line("pytest", root, env)
    assert "grpc-coverage" in out and re.search(r"^total +methods 4/11", out, re.MULTILINE), out


def test_readme_toml_fragments_load(tmp_path):
    for number, fragment in enumerate(toml_fragments()):
        path = tmp_path / str(number) / "pyproject.toml"
        path.parent.mkdir()
        path.write_text(fragment)
        load_config(path)


def test_every_python_example_is_a_file_of_a_project():
    for kind, _, lang, body in blocks():
        if lang in ("python", "shell", "yaml"):
            assert kind, f"an example that is never run:\n{body}"
