"""`[tool.grpc-coverage]` in `pyproject.toml`: where it is, what it says, and the absolute paths derived from it.

Paths are never resolved against the current directory: the observer looks for the config next to the file that
created it, the pytest plugin next to `rootdir`, the CLI next to cwd. In all cases relative paths are taken from the
folder of the `pyproject.toml` that holds the section.
"""

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc

logger = logging.getLogger("grpc_coverage")

SECTION = "grpc-coverage"
DEFAULT_RESULTS_DIR = "grpc-coverage-results"
DEFAULT_REPORT_DIR = "grpc-coverage-report"
DEFAULT_EXCLUDE = ("grpc.reflection.*", "grpc.health.*")
DEFAULT_REFLECTION_TIMEOUT = 10.0

ENV_RESULTS_DIR = "GRPC_COVERAGE_RESULTS_DIR"
ENV_REPORT_DIR = "GRPC_COVERAGE_REPORT_DIR"
ENV_FAIL_UNDER = "GRPC_COVERAGE_FAIL_UNDER"

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_STATUS_CODES = {code.name for code in grpc.StatusCode}


class ConfigError(ValueError):
    """Invalid `[tool.grpc-coverage]`: unknown key, wrong type, bad server name. Always names the key."""


@dataclass
class ServerConfig:
    """`[tool.grpc-coverage.<name>]`: one server, i.e. one `target` (a microservice), with its proto services."""

    name: str
    target: str | None = None
    tls: bool = False
    root_cert: Path | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    proto: list[Path] = field(default_factory=list)
    proto_include: list[Path] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    fail_under: float | None = None
    fail_under_contracts: float | None = None
    expected_codes: dict[str, list[str]] = field(default_factory=dict)
    # `${VAR}` without a value, per key. Breaks only what needs that key: a snapshot by `target`, a `proto` build.
    env_errors: dict[str, str] = field(default_factory=dict)

    @property
    def snapshot_error(self) -> str | None:
        """Why a snapshot by `target` is impossible right now (an unset env variable), if it is."""
        for key in ("target", "tls", "root-cert", "metadata"):
            if key in self.env_errors:
                return self.env_errors[key]
        return None


@dataclass
class Config:
    """Everything the observer, the plugin and the CLI need, with absolute paths."""

    root: Path
    # The `pyproject.toml` the section came from, `None` if none was found (then `root` is a fallback).
    pyproject: Path | None
    results_dir: Path
    report_dir: Path
    title: str
    exclude: list[str]
    fail_under: float | None
    fail_under_contracts: float | None
    reflection_timeout: float
    report_on_finish: bool
    servers: dict[str, ServerConfig]

    def server_exclude(self, server: ServerConfig) -> list[str]:
        return [*self.exclude, *server.exclude]


# key -> (attribute, kind). Kinds are checked by `_check`.
_GLOBAL_KEYS: dict[str, tuple[str, str]] = {
    "results-dir": ("results_dir", "str"),
    "report-dir": ("report_dir", "str"),
    "title": ("title", "str"),
    "exclude": ("exclude", "list[str]"),
    "fail-under": ("fail_under", "percent"),
    "fail-under-contracts": ("fail_under_contracts", "percent"),
    "reflection-timeout": ("reflection_timeout", "positive"),
    "report-on-finish": ("report_on_finish", "bool"),
}

_SERVER_KEYS: dict[str, tuple[str, str]] = {
    "target": ("target", "str"),
    "tls": ("tls", "bool"),
    "root-cert": ("root_cert", "str"),
    "metadata": ("metadata", "dict[str]"),
    "proto": ("proto", "str|list[str]"),
    "proto-include": ("proto_include", "list[str]"),
    "include": ("include", "list[str]"),
    "exclude": ("exclude", "list[str]"),
    "fail-under": ("fail_under", "percent"),
    "fail-under-contracts": ("fail_under_contracts", "percent"),
    "expected-codes": ("expected_codes", "codes"),
}


def has_section(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data.get("tool"), dict) and SECTION in data["tool"]


def find_pyproject(start: Path) -> Path | None:
    """The nearest `pyproject.toml` from `start` upwards that has `[tool.grpc-coverage]`.

    Files without the section are skipped, so a nested package of a monorepo or `~/pyproject.toml` can't take over.
    """
    start = start.resolve()
    for folder in (start, *start.parents):
        candidate = folder / "pyproject.toml"
        if candidate.is_file() and has_section(candidate):
            return candidate
    return None


def load_config(
    config: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    results_dir: str | os.PathLike[str] | None = None,
    report_dir: str | os.PathLike[str] | None = None,
    fail_under: float | None = None,
    fail_under_contracts: float | None = None,
) -> Config:
    """Find and read the config.

    `config` is an explicit `pyproject.toml` (or a folder with one); otherwise it is searched from `start` upwards.
    Keyword overrides are CLI flags: flag > environment variable > `[tool.grpc-coverage]` > default.
    """
    env = os.environ if env is None else env
    start_path = Path(start) if start is not None else Path.cwd()
    if config is not None:
        pyproject: Path | None = Path(config).expanduser().resolve()
        if pyproject.is_dir():
            pyproject = pyproject / "pyproject.toml"
        if not pyproject.is_file():
            raise ConfigError(f"config file not found: {pyproject}")
        root = pyproject.parent
    else:
        pyproject = find_pyproject(start_path)
        root = pyproject.parent if pyproject else start_path.resolve()
        if pyproject is None:
            logger.warning(
                "grpc-coverage: no pyproject.toml with [tool.%s] above %s; using %s as the project root",
                SECTION,
                start_path.resolve(),
                root,
            )

    data: dict[str, Any] = {}
    project_name = None
    if pyproject is not None:
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{pyproject}: invalid TOML: {exc}") from exc
        data = document.get("tool", {}).get(SECTION, {})
        if not isinstance(data, dict):
            raise ConfigError(f"[tool.{SECTION}] must be a table")
        project = document.get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            project_name = project["name"]

    values: dict[str, Any] = {}
    servers: dict[str, ServerConfig] = {}
    for key, value in data.items():  # TOML tables keep file order: this is the order of servers in the report
        if isinstance(value, dict):
            servers[key] = _server(key, value, root, env)
        elif key in _GLOBAL_KEYS:
            values[_GLOBAL_KEYS[key][0]] = _check(f"[tool.{SECTION}] {key}", value, _GLOBAL_KEYS[key][1])
        elif key in _SERVER_KEYS:
            raise ConfigError(
                f"[tool.{SECTION}] {key}: this key belongs to a server table, "
                f"e.g. [tool.{SECTION}.<server>] {key} = ..."
            )
        else:
            raise ConfigError(f"[tool.{SECTION}] {key}: unknown key; known keys: {', '.join(_GLOBAL_KEYS)}")

    env_fail_under = env.get(ENV_FAIL_UNDER)
    if fail_under is None and env_fail_under not in (None, ""):
        try:
            fail_under = _check(ENV_FAIL_UNDER, float(env_fail_under), "percent")
        except ValueError as exc:
            raise ConfigError(f"{ENV_FAIL_UNDER}={env_fail_under!r}: expected a number from 0 to 100") from exc
    if fail_under is not None:
        values["fail_under"] = _check("--fail-under", fail_under, "percent")
    if fail_under_contracts is not None:
        values["fail_under_contracts"] = _check("--fail-under-contracts", fail_under_contracts, "percent")

    return Config(
        root=root,
        pyproject=pyproject,
        results_dir=_dir(results_dir, env.get(ENV_RESULTS_DIR), values.get("results_dir"), DEFAULT_RESULTS_DIR, root),
        report_dir=_dir(report_dir, env.get(ENV_REPORT_DIR), values.get("report_dir"), DEFAULT_REPORT_DIR, root),
        title=values.get("title") or project_name or "gRPC coverage",
        exclude=list(values.get("exclude", DEFAULT_EXCLUDE)),
        fail_under=values.get("fail_under"),
        fail_under_contracts=values.get("fail_under_contracts"),
        reflection_timeout=float(values.get("reflection_timeout", DEFAULT_REFLECTION_TIMEOUT)),
        report_on_finish=values.get("report_on_finish", False),
        servers=servers,
    )


def _dir(flag: Any, env_value: str | None, toml_value: str | None, default: str, root: Path) -> Path:
    """Flag (relative to cwd: the user typed it) > env > toml > default (relative to the project root)."""
    if flag is not None:
        return Path(flag).expanduser().resolve()
    value = env_value or toml_value or default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _server(name: str, table: dict[str, Any], root: Path, env: Mapping[str, str]) -> ServerConfig:
    where = f"[tool.{SECTION}.{name}]"
    if name in _GLOBAL_KEYS:
        raise ConfigError(f"{where}: server name {name!r} clashes with the common key {name!r}; rename the server")
    if name in _SERVER_KEYS:
        hint = f"[tool.{SECTION}.<server>.{name}]" if _SERVER_KEYS[name][1] in ("codes", "dict[str]") else name
        raise ConfigError(
            f"{where}: {name!r} is a server key, not a server; write {hint} inside the table of a server, "
            f"[tool.{SECTION}.<server>]"
        )
    if not _SERVER_NAME.match(name):
        raise ConfigError(f"{where}: server name may contain only latin letters, digits, '-' and '_'")

    server = ServerConfig(name=name)
    for key, value in table.items():
        if key not in _SERVER_KEYS:
            raise ConfigError(f"{where} {key}: unknown key; known keys: {', '.join(_SERVER_KEYS)}")
        attribute, kind = _SERVER_KEYS[key]
        value = _check(f"{where} {key}", value, kind)
        try:
            value = _substitute(value, env)
        except _UnsetVariable as exc:
            server.env_errors[key] = f"{where} {key}: environment variable {exc.name} is not set"
            continue
        setattr(server, attribute, value)

    if server.root_cert is not None:
        server.root_cert = _path(root, server.root_cert)
    proto = server.proto
    server.proto = [_path(root, p) for p in ([proto] if isinstance(proto, str) else proto)]
    server.proto_include = [_path(root, p) for p in server.proto_include]
    return server


def _path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


class _UnsetVariable(Exception):
    def __init__(self, name: str):
        self.name = name


def substitute_env(value: str, env: Mapping[str, str]) -> str:
    """`${VAR}` and `${VAR:-default}`; an unset variable without a default raises `_UnsetVariable`."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        found = env.get(name)
        if found not in (None, ""):
            return found
        if default is not None:
            return default
        if found is not None:
            return found
        raise _UnsetVariable(name)

    return _ENV_REF.sub(replace, value)


def _substitute(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return substitute_env(value, env)
    if isinstance(value, list):
        return [_substitute(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, env) for key, item in value.items()}
    return value


def _check(where: str, value: Any, kind: str) -> Any:
    """Validate a TOML value against `kind`, raising `ConfigError` with the key name."""

    def fail(expected: str) -> ConfigError:
        return ConfigError(f"{where}: expected {expected}, got {type(value).__name__} {value!r}")

    if kind == "str":
        if not isinstance(value, str):
            raise fail("a string")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise fail("true or false")
    elif kind in ("percent", "positive"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise fail("a number")
        if kind == "percent" and not 0 <= value <= 100:
            raise fail("a number from 0 to 100")
        if kind == "positive" and value <= 0:
            raise fail("a positive number")
        value = float(value)
    elif kind == "list[str]":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise fail("a list of strings")
    elif kind == "str|list[str]":
        if not isinstance(value, str) and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            raise fail("a path or a list of paths")
    elif kind == "dict[str]":
        if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
            raise fail("a table of strings")
    elif kind == "codes":
        if not isinstance(value, dict):
            raise fail('a table like {"/pkg.Service/Method" = ["NOT_FOUND"]}')
        for method, codes in value.items():
            if not method.startswith("/") or method.count("/") != 2:
                raise ConfigError(f"{where} {method!r}: expected a full method name, /package.Service/Method")
            if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
                raise ConfigError(f"{where} {method!r}: expected a list of status code names")
            unknown = [code for code in codes if code not in _STATUS_CODES]
            if unknown:
                raise ConfigError(f"{where} {method!r}: unknown status codes {unknown}; e.g. NOT_FOUND")
    else:  # pragma: no cover
        raise AssertionError(kind)
    return value
