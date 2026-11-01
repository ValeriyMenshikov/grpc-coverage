"""Contract elements of a request: field paths, enum values and oneof branches.

`collect` walks a live message (what the tests sent), `elements` walks a descriptor (what the schema allows).
Both follow the same rules, so what one produces the other can find:

- nested messages are walked recursively, path `user.address.city`;
- a recursive type stops where it repeats on the current path: `children` is an element, `children.children` is not;
- repeated and map fields are covered when not empty; their message elements / values are walked like a nested
  message, without an index in the path;
- well-known types (`google.protobuf.*`: Timestamp, Duration, wrappers, Struct, Any, ...) are leaves;
- enum values are `path=NAME`, the zero value (`*_UNSPECIFIED`) never counts: proto3 can't tell it from "not set";
- oneof branches are `path:branch`, where `path` is the oneof itself (`payload:card`, `user.contact:email`).
"""

from dataclasses import dataclass, field

from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor, OneofDescriptor
from google.protobuf.message import Message

_LABEL_REPEATED = 3


@dataclass
class Collected:
    """Contract elements seen in the requests of one call."""

    request_fields: set[str] = field(default_factory=set)
    enum_values: set[str] = field(default_factory=set)
    oneofs: set[str] = field(default_factory=set)


@dataclass
class FieldElement:
    path: str
    type: str
    # proto3 scalar without `optional`: its default value (0, "", false) is not sent, so it can't be seen.
    implicit_default: bool = False


@dataclass
class Elements:
    """Contract elements a request type allows, in schema order."""

    fields: list[FieldElement] = field(default_factory=list)
    enum_values: list[str] = field(default_factory=list)
    oneofs: list[str] = field(default_factory=list)


def is_well_known(descriptor: Descriptor) -> bool:
    return descriptor.full_name.startswith("google.protobuf.")


def is_repeated(field_descriptor: FieldDescriptor) -> bool:
    repeated = getattr(field_descriptor, "is_repeated", None)  # protobuf >= 6, `label` is deprecated there
    if repeated is not None:
        return bool(repeated)
    return field_descriptor.label == _LABEL_REPEATED


def is_map(field_descriptor: FieldDescriptor) -> bool:
    message_type = field_descriptor.message_type
    return message_type is not None and message_type.GetOptions().map_entry


def real_oneofs(descriptor: Descriptor) -> list[OneofDescriptor]:
    """Oneofs written in the .proto, without the synthetic `_name` oneofs of proto3 `optional` fields."""
    return [
        oneof
        for oneof in descriptor.oneofs
        if not (len(oneof.fields) == 1 and oneof.name == "_" + oneof.fields[0].name)
    ]


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _enum_name(enum: EnumDescriptor, number: int) -> str:
    value = enum.values_by_number.get(number)
    return value.name if value is not None else str(number)


def collect(message: Message, into: Collected | None = None) -> Collected:
    """Add the contract elements present in `message` to `into`. Uses only `message.DESCRIPTOR` and `ListFields()`."""
    into = into if into is not None else Collected()
    _collect(message, "", (message.DESCRIPTOR.full_name,), into)
    return into


def _collect(message: Message, prefix: str, types: tuple[str, ...], into: Collected) -> None:
    descriptor = message.DESCRIPTOR
    for oneof in real_oneofs(descriptor):
        branch = message.WhichOneof(oneof.name)
        if branch:
            into.oneofs.add(f"{_join(prefix, oneof.name)}:{branch}")
    for field_descriptor, value in message.ListFields():
        if field_descriptor.is_extension:
            continue
        path = _join(prefix, field_descriptor.name)
        into.request_fields.add(path)
        if is_map(field_descriptor):
            value_field = field_descriptor.message_type.fields_by_name["value"]
            _collect_values(value_field, path, types, list(value.values()), into)
        elif is_repeated(field_descriptor):
            _collect_values(field_descriptor, path, types, list(value), into)
        else:
            _collect_values(field_descriptor, path, types, [value], into)


def _collect_values(
    field_descriptor: FieldDescriptor, path: str, types: tuple[str, ...], values: list, into: Collected
) -> None:
    if field_descriptor.enum_type is not None:
        for number in values:
            if number != 0:
                into.enum_values.add(f"{path}={_enum_name(field_descriptor.enum_type, number)}")
        return
    message_type = field_descriptor.message_type
    if message_type is None or is_well_known(message_type) or message_type.full_name in types:
        return
    for value in values:
        _collect(value, path, (*types, message_type.full_name), into)


def elements(descriptor: Descriptor) -> Elements:
    """All contract elements of a request type, by the same rules as `collect`."""
    result = Elements()
    _elements(descriptor, "", (descriptor.full_name,), result)
    return result


def _elements(descriptor: Descriptor, prefix: str, types: tuple[str, ...], into: Elements) -> None:
    for oneof in real_oneofs(descriptor):
        for branch in oneof.fields:
            into.oneofs.append(f"{_join(prefix, oneof.name)}:{branch.name}")
    for field_descriptor in descriptor.fields:
        path = _join(prefix, field_descriptor.name)
        target = field_descriptor
        if is_map(field_descriptor):
            target = field_descriptor.message_type.fields_by_name["value"]
            type_name = f"map<{_type_name(field_descriptor.message_type.fields_by_name['key'])}, {_type_name(target)}>"
        elif is_repeated(field_descriptor):
            type_name = f"repeated {_type_name(field_descriptor)}"
        else:
            type_name = _type_name(field_descriptor)
        implicit_default = (
            not is_repeated(field_descriptor)
            and field_descriptor.message_type is None
            and not field_descriptor.has_presence
        )
        into.fields.append(FieldElement(path=path, type=type_name, implicit_default=implicit_default))
        if target.enum_type is not None:
            into.enum_values.extend(f"{path}={value.name}" for value in target.enum_type.values if value.number != 0)
            continue
        message_type = target.message_type
        if message_type is None or is_well_known(message_type) or message_type.full_name in types:
            continue
        _elements(message_type, path, (*types, message_type.full_name), into)


_SCALAR_NAMES = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
}


def _type_name(field_descriptor: FieldDescriptor) -> str:
    if field_descriptor.message_type is not None:
        return field_descriptor.message_type.full_name
    if field_descriptor.enum_type is not None:
        return field_descriptor.enum_type.full_name
    return _SCALAR_NAMES.get(field_descriptor.type, "?")
