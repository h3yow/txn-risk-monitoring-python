"""规则集校验与版本化管理。

规则以 JSON 风格的 dict 批量提交，本模块负责结构与取值校验：
任一条规则非法都抛出 :class:`RuleValidationError`，整批拒绝，
不返回任何部分结果，由调用方保证"不生效"，杜绝部分生效或静默降级。

规则参数契约（params）：
- ``amount_limit``: ``{"limit": 正数, "score": 非负整数}``
    单笔金额 > limit 时命中。
- ``velocity``: ``{"window_minutes": >=1 整数, "max_count": >=1 整数,
    "score": 非负整数}`` 同一账户在时间窗内（含当前笔）交易笔数 > max_count 时命中。
- ``blocklist``: ``{"counterparties": 非空字符串列表, "score": 非负整数}``
    交易对手命中黑名单时命中。
"""

from __future__ import annotations

from typing import Any

from app.models import Rule, RuleType, RuleValidationError


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_int(v: Any) -> bool:
    # bool 是 int 的子类，规则里显式排除，避免 True 被当成 1
    return isinstance(v, int) and not _is_bool(v)


def _is_number(v: Any) -> bool:
    return (isinstance(v, float) or _is_int(v)) and not _is_bool(v)


def _validate_score(params: dict[str, Any], ctx: str) -> None:
    score = params.get("score")
    if not _is_int(score) or score < 0:
        raise ValueError(f"{ctx}.score 必须是非负整数，实际为 {score!r}")


def _validate_amount_limit(params: dict[str, Any], ctx: str) -> None:
    limit = params.get("limit")
    if not _is_number(limit) or limit <= 0:
        raise ValueError(f"{ctx}.params.limit 必须是正数，实际为 {limit!r}")
    _validate_score(params, ctx)


def _validate_velocity(params: dict[str, Any], ctx: str) -> None:
    window = params.get("window_minutes")
    if not _is_int(window) or window < 1:
        raise ValueError(
            f"{ctx}.params.window_minutes 必须是 >=1 的整数，实际为 {window!r}"
        )
    max_count = params.get("max_count")
    if not _is_int(max_count) or max_count < 1:
        raise ValueError(
            f"{ctx}.params.max_count 必须是 >=1 的整数，实际为 {max_count!r}"
        )
    _validate_score(params, ctx)


def _validate_blocklist(params: dict[str, Any], ctx: str) -> None:
    cps = params.get("counterparties")
    if not isinstance(cps, list) or not cps:
        raise ValueError(f"{ctx}.params.counterparties 必须是非空列表")
    if not all(isinstance(c, str) and c.strip() for c in cps):
        raise ValueError(f"{ctx}.params.counterparties 中的每一项都必须是非空字符串")
    if len(set(cps)) != len(cps):
        raise ValueError(f"{ctx}.params.counterparties 不得包含重复项")
    _validate_score(params, ctx)


_PARAM_VALIDATORS = {
    RuleType.AMOUNT_LIMIT: _validate_amount_limit,
    RuleType.VELOCITY: _validate_velocity,
    RuleType.BLOCKLIST: _validate_blocklist,
}


def validate_rules(payload: list[dict]) -> tuple[Rule, ...]:
    """校验并构造规则元组；任一规则非法则整批拒绝。

    :raises RuleValidationError: 顶层不是列表、rule_id 缺失/重复、
        rule_type 不支持、enabled 非布尔、params 非 dict 或参数取值非法。
    """
    if not isinstance(payload, list) or not payload:
        raise RuleValidationError("规则集必须是非空列表")

    seen_ids: set[str] = set()
    rules: list[Rule] = []
    for index, raw in enumerate(payload):
        ctx = f"rules[{index}]"
        try:
            if not isinstance(raw, dict):
                raise ValueError("规则必须是对象(dict)")
            rule_id = raw.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError("rule_id 必须是非空字符串")
            if rule_id in seen_ids:
                raise ValueError(f"rule_id {rule_id!r} 在批次内重复")
            seen_ids.add(rule_id)

            raw_type = raw.get("rule_type")
            try:
                rule_type = RuleType(raw_type)
            except ValueError:
                supported = ", ".join(t.value for t in RuleType)
                raise ValueError(
                    f"rule_type {raw_type!r} 不支持，支持的类型: {supported}"
                )

            enabled = raw.get("enabled", True)
            if not _is_bool(enabled):
                raise ValueError(f"enabled 必须是布尔值，实际为 {enabled!r}")

            params = raw.get("params")
            if not isinstance(params, dict):
                raise ValueError("params 必须是对象(dict)")

            _PARAM_VALIDATORS[rule_type](params, ctx)
        except (ValueError, KeyError, TypeError) as exc:
            raise RuleValidationError(f"非法规则，整批拒绝: {exc}") from exc

        rules.append(
            Rule(rule_id=rule_id, rule_type=rule_type, params=dict(params), enabled=enabled)
        )

    return tuple(rules)
