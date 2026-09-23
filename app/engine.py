"""规则校验与评估引擎。

规则三类：
- amount_threshold：交易金额 >= threshold 时加 score
- blacklist：收款人 payee 命中 payees 名单时加 score
- high_risk_country：交易发起国 country 命中 countries 名单时加 score

分值汇总：< REVIEW_SCORE => allow；>= BLOCK_SCORE => block；其余 => review。
评估只依赖传入的 RuleSetVersion，因此同一输入在同一版本下结果必然可复现。
"""
from __future__ import annotations

from typing import Any

from app.errors import RuleBatchInvalidError
from app.models import (
    Account,
    Decision,
    Rule,
    RuleHit,
    RuleSetVersion,
    RuleStatus,
    RuleType,
    TransactionInput,
)

REVIEW_SCORE = 50
BLOCK_SCORE = 100

_REQUIRED_PARAMS: dict[RuleType, dict[str, tuple[type, ...]]] = {
    RuleType.AMOUNT_THRESHOLD: {"threshold": (int, float), "score": (int,)},
    RuleType.BLACKLIST: {"payees": (list,), "score": (int,)},
    RuleType.HIGH_RISK_COUNTRY: {"countries": (list,), "score": (int,)},
}


def validate_rules(raw_rules: list[dict[str, Any]]) -> list[Rule]:
    """整批校验规则。

    任一规则结构/取值非法即抛 RuleBatchInvalidError，错误明细列出全部问题，
    由调用方保证整批不生效（不写入任何新版本）。
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    normalized: list[Rule] = []

    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleBatchInvalidError(
            "规则批次为空或格式非法", details={"errors": ["rules must be a non-empty list"]}
        )

    for index, raw in enumerate(raw_rules):
        prefix = f"rules[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{prefix}: 必须是对象")
            continue

        rule_id = raw.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{prefix}: rule_id 必须是非空字符串")
        elif rule_id in seen_ids:
            errors.append(f"{prefix}: rule_id '{rule_id}' 重复")
        else:
            seen_ids.add(rule_id)

        type_raw = raw.get("rule_type")
        try:
            rule_type = RuleType(type_raw)
        except (ValueError, TypeError):
            errors.append(
                f"{prefix}: rule_type '{type_raw}' 非法，"
                f"允许值: {[t.value for t in RuleType]}"
            )
            continue

        params = raw.get("params")
        if not isinstance(params, dict):
            errors.append(f"{prefix}: params 必须是对象")
            continue

        spec = _REQUIRED_PARAMS[rule_type]
        for key, allowed_types in spec.items():
            if key not in params:
                errors.append(f"{prefix}: 缺少参数 '{key}'")
                continue
            value = params[key]
            # bool 是 int 的子类，显式排除
            if isinstance(value, bool) or not isinstance(value, allowed_types):
                type_names = "/".join(t.__name__ for t in allowed_types)
                errors.append(f"{prefix}: 参数 '{key}' 类型必须为 {type_names}")
                continue
            if key == "score" and value <= 0:
                errors.append(f"{prefix}: score 必须为正整数")
            if key == "threshold" and value <= 0:
                errors.append(f"{prefix}: threshold 必须为正数")
            if key in ("payees", "countries"):
                if not value or not all(isinstance(v, str) and v for v in value):
                    errors.append(f"{prefix}: {key} 必须为非空字符串数组")

        extra = set(params) - set(spec)
        if extra:
            errors.append(f"{prefix}: 存在未知参数 {sorted(extra)}")

        if not any(e.startswith(prefix) for e in errors):
            normalized.append(
                Rule(
                    rule_id=rule_id,
                    rule_type=rule_type,
                    params=dict(params),
                    status=RuleStatus.ACTIVE,
                    description=str(raw.get("description", "")),
                )
            )

    if errors:
        raise RuleBatchInvalidError(
            f"规则批次整批拒绝：{len(errors)} 处非法", details={"errors": errors}
        )
    return normalized


def evaluate(
    txn: TransactionInput,
    account: Account | None,
    rule_set: RuleSetVersion,
) -> tuple[Decision, int, list[RuleHit]]:
    hits: list[RuleHit] = []
    for rule in rule_set.rules:
        if rule.status is not RuleStatus.ACTIVE:
            continue
        hit = _eval_rule(rule, txn, account, rule_set.version)
        if hit is not None:
            hits.append(hit)

    total = sum(h.score for h in hits)
    if total >= BLOCK_SCORE:
        decision = Decision.BLOCK
    elif total >= REVIEW_SCORE:
        decision = Decision.REVIEW
    else:
        decision = Decision.ALLOW
    return decision, total, hits


def _eval_rule(
    rule: Rule,
    txn: TransactionInput,
    account: Account | None,
    version: int,
) -> RuleHit | None:
    p = rule.params
    if rule.rule_type is RuleType.AMOUNT_THRESHOLD:
        if txn.amount >= p["threshold"]:
            return RuleHit(
                rule_id=rule.rule_id,
                rule_type=rule.rule_type,
                rule_version=version,
                score=int(p["score"]),
                reason=f"金额 {txn.amount} >= 阈值 {p['threshold']}",
            )
    elif rule.rule_type is RuleType.BLACKLIST:
        if txn.payee in p["payees"]:
            return RuleHit(
                rule_id=rule.rule_id,
                rule_type=rule.rule_type,
                rule_version=version,
                score=int(p["score"]),
                reason=f"收款人 '{txn.payee}' 命中黑名单",
            )
    elif rule.rule_type is RuleType.HIGH_RISK_COUNTRY:
        country = txn.country
        if country in p["countries"]:
            return RuleHit(
                rule_id=rule.rule_id,
                rule_type=rule.rule_type,
                rule_version=version,
                score=int(p["score"]),
                reason=f"国家/地区 '{country}' 命中高风险地区",
            )
    return None
