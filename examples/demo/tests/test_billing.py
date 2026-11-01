import grpc
import pytest
from billing.v1 import billing_pb2


def test_charge_card(billing):
    money = billing_pb2.Money(units=100, currency=billing_pb2.CURRENCY_EUR)
    request = billing_pb2.ChargeRequest(account_id="u-ann", amount=money, card=billing_pb2.Card(number="4242"))
    assert billing.Charge(request).charge_id


def test_charge_zero(billing):
    with pytest.raises(grpc.RpcError) as error:
        billing.Charge(billing_pb2.ChargeRequest(account_id="u-ann"))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_refund(billing):
    assert billing.Refund(billing_pb2.RefundRequest(charge_id="c-1")).charge_id == "c-1"
