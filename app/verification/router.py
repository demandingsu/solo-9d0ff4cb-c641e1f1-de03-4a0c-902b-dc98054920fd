"""峰谷标签核查路由（注册到 ``app.main:app`` 同一应用实例）。

流程：抓取供电商用电明细分页列表与逐日明细 -> 解析起止时间 / 用电量 / 原标签 /
详情来源 -> 按当地日期匹配生效的峰谷时段规则（允许跨午夜）-> 仅当采样区间完整落在
唯一有效时段内才判定标签对错；其余记录列为待核定，电量原样汇总不拆分。
任何列表或明细抓取失败都会列出失败地址，整份报告标记为“待核定”。
"""
from __future__ import annotations

from fastapi import FastAPI

from .label_check import (
    EffectiveRule,
    JudgedDecision,
    PendingDecision,
    RulePeriod,
    verify_records,
)
from .schemas import (
    EnergyTotalsOut,
    FailureOut,
    RecordSourceOut,
    RuleRefOut,
    UsageRecordOut,
    VerificationReportOut,
    VerifyLabelsRequest,
)
from .usage_scraper import crawl_usage_site


def _rule_refs(rules: list[EffectiveRule]) -> list[RuleRefOut]:
    return [
        RuleRefOut(rule_id=rule.rule_id, effective_from=rule.effective_from, effective_to=rule.effective_to)
        for rule in sorted(rules, key=lambda r: (r.effective_from, r.rule_id))
    ]


def _build_rules(request: VerifyLabelsRequest) -> list[EffectiveRule]:
    return [
        EffectiveRule(
            rule_id=rule.rule_id,
            effective_from=rule.effective_from,
            effective_to=rule.effective_to,
            periods=tuple(
                RulePeriod(label=period.label, start=period.start, end=period.end)
                for period in rule.periods
            ),
        )
        for rule in request.rules
    ]


def _record_out(verdict, rule_refs: dict[str, RuleRefOut]) -> UsageRecordOut:
    record = verdict.record
    decision = verdict.decision
    expected = None
    pending_reason = None
    pending_detail = None
    period_start = None
    period_end = None
    source_rules: list[RuleRefOut] = []

    if isinstance(decision, JudgedDecision):
        expected = decision.expected_label
        period_start = decision.valid_period_start
        period_end = decision.valid_period_end
        source_rules = [rule_refs[rule_id] for rule_id in decision.source_rule_ids if rule_id in rule_refs]
        status = "mislabeled" if decision.is_mislabeled else "correct"
    elif isinstance(decision, PendingDecision):
        pending_reason = decision.reason.value
        pending_detail = decision.detail
        status = "pending"
    else:  # pragma: no cover - 引擎只会返回上述两种决策
        raise TypeError(f"未知核查决策类型: {type(decision)!r}")

    return UsageRecordOut(
        detail_id=record.detail_id,
        local_date=record.local_date,
        interval_start=record.start,
        interval_end=record.end,
        energy_kwh=float(record.energy_kwh),
        original_label=record.original_label,
        expected_label=expected,
        status=status,
        pending_reason=pending_reason,
        pending_detail=pending_detail,
        valid_period_start=period_start,
        valid_period_end=period_end,
        source_rules=source_rules,
        source=RecordSourceOut(
            detail_id=record.detail_id,
            detail_url=record.detail_url,
            list_url=record.list_url,
            source_range_start=record.source_range_start,
            source_range_end=record.source_range_end,
        ),
    )


def register(app: FastAPI) -> None:
    @app.post("/api/v1/verify-labels", response_model=VerificationReportOut, tags=["标签核查"])
    def verify_labels(request: VerifyLabelsRequest) -> VerificationReportOut:
        crawl_result = crawl_usage_site(request.list_url, timeout=request.timeout_seconds)
        rules = _build_rules(request)
        rule_refs = {ref.rule_id: ref for ref in _rule_refs(rules)}

        outcome = verify_records(crawl_result.records, rules)
        mislabeled = [_record_out(verdict, rule_refs) for verdict in outcome.mislabeled]
        pending = [_record_out(verdict, rule_refs) for verdict in outcome.pending]

        failures = [
            FailureOut(url=failure.url, stage=failure.stage, reason=failure.reason, status=failure.status)
            for failure in crawl_result.failures
        ]
        empty_details = list(crawl_result.empty_detail_urls)

        blocking = bool(failures) or bool(empty_details) or bool(pending)
        if blocking:
            parts = []
            if failures:
                parts.append(f"{len(failures)} 个列表页或明细页抓取失败")
            if empty_details:
                parts.append(f"{len(empty_details)} 个明细页没有任何用电记录")
            if pending:
                parts.append(f"{len(pending)} 条记录无法唯一核定")
            note = "；".join(parts) + "，整份报告待核定，错标明细仅供联系供电商时参考"
        else:
            note = "全部明细抓取成功且每条采样区间均完整落在唯一有效时段内，核查结论已核定"

        return VerificationReportOut(
            list_url=request.list_url,
            verification_status="pending_verification" if blocking else "verified",
            note=note,
            crawl={
                "visited_list_urls": crawl_result.visited_list_urls,
                "details_crawled": len(crawl_result.details),
                "empty_detail_count": len(empty_details),
                "empty_detail_urls": empty_details,
                "records_crawled": len(crawl_result.records),
                "mislabeled_count": len(mislabeled),
                "pending_count": len(pending),
                "failure_count": len(failures),
            },
            rules_applied=_rule_refs(rules),
            totals=EnergyTotalsOut(
                total_kwh=float(outcome.known_total_kwh),
                verifiable_kwh=float(outcome.verifiable_kwh),
                correct_kwh=float(outcome.correct_kwh),
                mislabeled_kwh=float(outcome.mislabeled_kwh),
                pending_kwh=float(outcome.pending_kwh),
            ),
            mislabeled_records=mislabeled,
            pending_records=pending,
            failures=failures,
        )
