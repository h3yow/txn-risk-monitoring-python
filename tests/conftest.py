"""pytest 公共夹具：开启 INFO 日志，单测日志会打印
交易标识、规则版本与判定依据（risk.engine / risk.events logger）。"""

import logging
from datetime import datetime, timezone

import pytest

from app.service import RiskMonitoringService

# v1 固定从 2000-01-01 生效：既早于用例中所有显式 submitted_at（2026 年内），
# 也早于任意一天执行测试的"当前时刻"，因此实时/补录结论都不依赖跑用例当天的日期。
V1_EFFECTIVE_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)

V1_RULES = [
    {
        "rule_id": "AMT-1000",
        "rule_type": "amount_limit",
        "params": {"limit": 1000, "score": 120},
    },
    {
        "rule_id": "VEL-2-5M",
        "rule_type": "velocity",
        "params": {"window_minutes": 5, "max_count": 2, "score": 50},
    },
    {
        "rule_id": "BL-1",
        "rule_type": "blocklist",
        "params": {"counterparties": ["BAD-CORP", "SANCTIONED-X"], "score": 200},
    },
]


@pytest.fixture(autouse=True)
def _enable_engine_logs(caplog):
    caplog.set_level(logging.INFO, logger="risk.engine")
    caplog.set_level(logging.INFO, logger="risk.events")
    caplog.set_level(logging.INFO, logger="risk.service")


@pytest.fixture
def svc() -> RiskMonitoringService:
    s = RiskMonitoringService()
    s.publish_rules(V1_RULES, note="初始版本 v1", effective_at=V1_EFFECTIVE_AT)
    s.create_account("ACC-1")
    s.create_account("ACC-2")
    return s
