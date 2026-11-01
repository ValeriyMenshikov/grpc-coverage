import grpc
import pytest
from account.v1 import account_pb2


def test_create(account):
    user = account_pb2.User(
        name="ann",
        status=account_pb2.STATUS_ACTIVE,
        address=account_pb2.Address(city="Lisbon"),
        tags=["vip"],
    )
    assert account.Create(account_pb2.CreateRequest(user=user, email="ann@example.com")).id == "u-ann"


def test_create_by_phone(account):
    user = account_pb2.User(name="bob", status=account_pb2.STATUS_BLOCKED, age=0)
    assert account.Create(account_pb2.CreateRequest(user=user, phone="+351000000")).id == "u-bob"


def test_create_without_name(account):
    with pytest.raises(grpc.RpcError) as error:
        account.Create(account_pb2.CreateRequest(user=account_pb2.User()))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_get(account):
    assert account.Get(account_pb2.GetRequest(id="u-1")).name == "u-1"


def test_list(account):
    request = account_pb2.ListRequest(limit=3, statuses=[account_pb2.STATUS_ACTIVE])
    assert len(list(account.List(request))) == 3


def test_chat(account):
    replies = account.Chat(iter([account_pb2.ChatMessage(text="hi"), account_pb2.ChatMessage(text="bye")]))
    assert [reply.text for reply in replies] == ["HI", "BYE"]
