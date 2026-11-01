import os

import grpc
import pytest
from account.v1 import account_pb2_grpc
from billing.v1 import billing_pb2_grpc

from grpc_coverage import CoverageObserver


def coverage_channel(server: str, target: str):
    coverage = CoverageObserver(server)
    channel = grpc.intercept_channel(grpc.insecure_channel(target), coverage.interceptor())
    coverage.snapshot(channel)
    return channel


@pytest.fixture(scope="session")
def account():
    channel = coverage_channel("account", os.environ["ACCOUNT_TARGET"])
    yield account_pb2_grpc.AccountStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def billing():
    channel = coverage_channel("billing", os.environ["BILLING_TARGET"])
    yield billing_pb2_grpc.BillingStub(channel)
    channel.close()


@pytest.fixture(scope="session", autouse=True)
def grpc_schemas(account, billing):
    """Schemas of both servers in every run."""
