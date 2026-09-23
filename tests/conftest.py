"""测试公共夹具：确保判定审计日志（交易标识/规则版本/判定依据）在单测中打印。"""
from __future__ import annotations

import logging

import pytest

from app.logging_config import configure_logging
from app.models import Account
from app.services import RiskService
from app.storage import Storage


@pytest.fixture(autouse=True)
def _audit_logs(caplog: pytest.LogCaptureFixture) -> None:
    logger = configure_logging(logging.INFO)
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="risk.audit")
    yield


@pytest.fixture
def service() -> RiskService:
    return RiskService(Storage())


@pytest.fixture
def service_with_account(service: RiskService) -> RiskService:
    service.create_account(Account(account_id="A1", country="CN", risk_level="normal"))
    return service


def make_txn(txn_id: str = "T1", **overrides):
    from app.models import TransactionInput

    defaults = dict(
        txn_id=txn_id, account_id="A1", amount=2000.0,
        currency="CNY", country="CN", payee="P1",
    )
    defaults.update(overrides)
    return TransactionInput(**defaults)
