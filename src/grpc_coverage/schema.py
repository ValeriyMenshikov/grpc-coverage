"""Schemas as `FileDescriptorSet`: from reflection responses, from `.proto` files (`grpc_tools.protoc`), into a pool."""

import fnmatch
import importlib.util
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import ServiceDescriptor


class SchemaError(Exception):
    """A schema could not be built: protoc failed, the extra is missing, a dependency is missing."""


def selected(name: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    """Whether a proto service (`account.v1.Account`) passes `include` (all if empty) and `exclude` globs."""
    include = list(include)
    if include and not any(fnmatch.fnmatchcase(name, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude)


def ordered(files: Iterable[descriptor_pb2.FileDescriptorProto]) -> list[descriptor_pb2.FileDescriptorProto]:
    """Dependencies first, so that the files can be added to a pool one by one."""
    by_name = {file.name: file for file in files}
    result: list[descriptor_pb2.FileDescriptorProto] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen or name not in by_name:
            return
        seen.add(name)
        for dependency in by_name[name].dependency:
            visit(dependency)
        result.append(by_name[name])

    for name in sorted(by_name):
        visit(name)
    return result


def descriptor_set(files: Iterable[descriptor_pb2.FileDescriptorProto]) -> descriptor_pb2.FileDescriptorSet:
    return descriptor_pb2.FileDescriptorSet(file=ordered(files))


def build_pool(file_set: descriptor_pb2.FileDescriptorSet) -> descriptor_pool.DescriptorPool:
    pool = descriptor_pool.DescriptorPool()
    names = {file.name for file in file_set.file}
    for file in ordered(file_set.file):
        missing = [dependency for dependency in file.dependency if dependency not in names]
        if missing:
            raise SchemaError(f"{file.name}: dependencies are missing from the schema: {', '.join(missing)}")
        pool.Add(file)
    return pool


def find_service(pool: descriptor_pool.DescriptorPool, name: str) -> ServiceDescriptor | None:
    try:
        return pool.FindServiceByName(name)
    except KeyError:
        return None


def services_in(file_set: descriptor_pb2.FileDescriptorSet, files: Iterable[str] | None = None) -> list[str]:
    """Full names of the services declared in `files` (all files of the set if `None`)."""
    wanted = None if files is None else set(files)
    return [
        f"{file.package}.{service.name}" if file.package else service.name
        for file in file_set.file
        if wanted is None or file.name in wanted
        for service in file.service
    ]


def compile_proto(proto: list[Path], include: list[Path]) -> tuple[descriptor_pb2.FileDescriptorSet, list[str]]:
    """Compile `.proto` files (folders are searched recursively) with `--include_imports`.

    Returns the set and the services declared in the given files themselves (not in their imports).
    """
    if importlib.util.find_spec("grpc_tools") is None:
        raise SchemaError("compiling .proto needs grpcio-tools: pip install 'grpc-coverage[proto]'")
    from importlib.resources import files as resource_files

    sources: list[Path] = []
    roots: list[Path] = []
    for path in proto:
        if path.is_dir():
            found = sorted(path.rglob("*.proto"))
            if not found:
                raise SchemaError(f"proto: no .proto files in {path}")
            sources.extend(found)
            roots.append(path)
        elif path.is_file():
            sources.append(path)
            roots.append(path.parent)
        else:
            raise SchemaError(f"proto: {path} does not exist")
    well_known = Path(str(resource_files("grpc_tools") / "_proto"))
    include_paths = list(dict.fromkeys([*roots, *include, well_known]))

    def relative(source: Path) -> str:
        for root in include_paths:
            if source.is_relative_to(root):
                return source.relative_to(root).as_posix()
        return source.name  # pragma: no cover - every source is under one of `roots`

    with tempfile.TemporaryDirectory(prefix="grpc-coverage-") as tmp:
        out = Path(tmp) / "set.binpb"
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            *(f"-I{root}" for root in include_paths),
            "--include_imports",
            f"--descriptor_set_out={out}",
            *(str(source) for source in sources),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise SchemaError(f"protoc failed:\n{(completed.stderr or completed.stdout).strip()}")
        file_set = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())
    return file_set, services_in(file_set, {relative(source) for source in sources})
