"""One test per walking rule, for both sides: live messages (`collect`) and the schema (`elements`)."""

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from helpers import modules

from grpc_coverage.paths import collect, elements


@pytest.fixture(scope="module")
def pb(gen):
    return modules()


def test_nested_messages(pb):
    account, _, _ = pb
    seen = collect(account.CreateRequest(user=account.User(address=account.Address(city="Paris"))))
    assert seen.request_fields == {"user", "user.address", "user.address.city"}
    contract = {f.path for f in elements(account.CreateRequest.DESCRIPTOR).fields}
    assert {"user", "user.address", "user.address.city", "user.address.street"} <= contract


def test_recursive_type_is_cut_where_it_repeats(pb):
    account, _, _ = pb
    tree = account.Node(name="root", children=[account.Node(name="a", children=[account.Node(name="b")])])
    assert collect(account.UploadRequest(tree=tree)).request_fields == {"tree", "tree.name", "tree.children"}
    assert [f.path for f in elements(account.UploadRequest.DESCRIPTOR).fields] == [
        "chunk",
        "tree",
        "tree.name",
        "tree.children",
    ]
    # the root type counts as "on the path" too
    assert [f.path for f in elements(account.Node.DESCRIPTOR).fields] == ["name", "children"]


def test_repeated_covered_when_not_empty_nested_from_all_elements(pb):
    account, _, _ = pb
    assert collect(account.User(tags=[])).request_fields == set()
    assert collect(account.User(tags=["a"])).request_fields == {"tags"}
    message = account.ChatMessage(text="x")
    message.addresses["home"].city = "Oslo"
    message.addresses["work"].street = "Main"
    assert collect(message).request_fields == {"text", "addresses", "addresses.city", "addresses.street"}


def test_map(pb):
    account, _, _ = pb
    user = account.User()
    assert collect(user).request_fields == set()
    user.labels["team"] = "core"
    assert collect(user).request_fields == {"labels"}
    fields = {f.path: f.type for f in elements(account.ChatMessage.DESCRIPTOR).fields}
    assert fields["addresses"] == "map<string, account.v1.Address>"
    assert "addresses.city" in fields  # map values that are messages are walked as nested


def test_enum_values_without_zero(pb):
    account, _, _ = pb
    seen = collect(account.CreateRequest(user=account.User(status=account.STATUS_ACTIVE)))
    assert seen.enum_values == {"user.status=STATUS_ACTIVE"}
    assert collect(account.User(status=account.STATUS_UNSPECIFIED)).enum_values == set()
    listing = collect(account.ListRequest(statuses=[account.STATUS_UNSPECIFIED, account.STATUS_BLOCKED]))
    assert listing.enum_values == {"statuses=STATUS_BLOCKED"}
    assert elements(account.ListRequest.DESCRIPTOR).enum_values == ["statuses=STATUS_ACTIVE", "statuses=STATUS_BLOCKED"]


def test_oneof_branches(pb):
    account, billing, _ = pb
    assert collect(account.CreateRequest(email="a@b")).oneofs == {"contact:email"}
    assert collect(account.CreateRequest()).oneofs == set()
    assert elements(account.CreateRequest.DESCRIPTOR).oneofs == ["contact:email", "contact:phone"]
    card = collect(billing.ChargeRequest(card=billing.Card(number="4242")))
    assert card.oneofs == {"method:card"} and card.request_fields == {"card", "card.number"}
    # proto3 `optional` creates a synthetic oneof `_age`: it is not a oneof of the contract
    assert collect(account.User(age=3)).oneofs == set()
    assert elements(account.User.DESCRIPTOR).oneofs == []


def test_well_known_types_are_leaves(pb):
    account, billing, _ = pb
    request = account.CreateRequest(created_at=Timestamp(seconds=10, nanos=5))
    assert collect(request).request_fields == {"created_at"}
    fields = {f.path for f in elements(billing.ChargeRequest.DESCRIPTOR).fields}
    assert "comment" in fields and not any(path.startswith("comment.") for path in fields)
    assert not any(
        path.startswith("created_at.") for path in {f.path for f in elements(account.CreateRequest.DESCRIPTOR).fields}
    )


def test_proto3_default_is_not_seen_and_marked(pb):
    account, _, _ = pb
    assert collect(account.GetRequest(id="")).request_fields == set()
    assert collect(account.User(age=0)).request_fields == {"age"}  # `optional` has presence
    marks = {f.path: f.implicit_default for f in elements(account.User.DESCRIPTOR).fields}
    assert marks["name"] is True and marks["status"] is True
    assert marks["age"] is False and marks["address"] is False and marks["tags"] is False


def test_streams_accumulate(pb):
    account, _, _ = pb
    seen = collect(account.UploadRequest(chunk=b"1"))
    collect(account.UploadRequest(tree=account.Node(name="n")), seen)
    assert seen.request_fields == {"chunk", "tree", "tree.name"}
