"""分时用电明细的峰谷标签核查引擎。

判定原则（保守、可审计）：

1. 时段规则按“当地日期”生效；规则中的时段可以跨午夜（如 22:00-06:00）。
2. 只有一条记录的完整采样区间 [起, 止) 落在唯一一个有效时段内，才判定原标签
   正确或错标。
3. 采样区间跨多个时段、当地日期没有生效规则、规则没有覆盖采样区间、或同日
   存在互相矛盾的规则时，整条记录列为“待核定”。
4. 待核定记录的电量原样计入待核定电量，绝不按时间比例拆分或猜测分摊。
5. 同一标签、时间上连续或重叠的时段视为一个有效时段（可跨越日期边界）。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

# 单条记录的合理采样跨度上限：超过 24 小时不再是 15 分钟粒度明细。
MAX_RECORD_SPAN = timedelta(hours=24)
ONE_MICROSECOND = timedelta(microseconds=1)
FOUR_PLACES = Decimal("0.0001")


def normalize_label(value: str) -> str:
    """标签比较键：仅做 Unicode 归一化、去空白与大小写折叠，不做语义猜测。"""
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True)
class RulePeriod:
    label: str  # 规则中书写、用于对外展示的标签
    start: time
    end: time  # 小于 start 表示跨午夜；等于 start 在请求校验阶段已拒绝

    @property
    def key(self) -> str:
        return normalize_label(self.label)


@dataclass(frozen=True)
class EffectiveRule:
    rule_id: str
    effective_from: date
    effective_to: date | None
    periods: tuple[RulePeriod, ...]

    def applies_on(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    @property
    def signature(self) -> tuple:
        return tuple(sorted(
            (period.key, period.start.hour, period.start.minute, period.start.second,
             period.end.hour, period.end.minute, period.end.second)
            for period in self.periods
        ))


class PendingReason(str, Enum):
    CROSS_PERIOD = "cross_period"
    MISSING_RULE = "missing_rule"
    MISSING_PERIOD = "missing_period"
    RULE_CONFLICT = "rule_conflict"
    INVALID_INTERVAL = "invalid_interval"


@dataclass(frozen=True)
class PendingDecision:
    reason: PendingReason
    detail: str


@dataclass(frozen=True)
class JudgedDecision:
    expected_label: str  # 规则中的展示标签
    expected_label_key: str
    valid_period_start: datetime
    valid_period_end: datetime
    source_rule_ids: tuple[str, ...]
    is_mislabeled: bool


Decision = JudgedDecision | PendingDecision


@dataclass
class _LabelInterval:
    start: datetime
    end: datetime
    label_key: str
    label_display: str
    rule_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _DateResolution:
    status: str  # ok | missing | conflict
    rule: EffectiveRule | None = None
    rules: tuple[EffectiveRule, ...] = ()
    detail: str = ""


def _segments(period: RulePeriod) -> list[tuple[int, int]]:
    """把单个时段展开为当日内不含跨午夜的分钟区间。"""
    start_min = period.start.hour * 60 + period.start.minute
    end_min = period.end.hour * 60 + period.end.minute
    if start_min < end_min:
        return [(start_min, end_min)]
    return [(start_min, 24 * 60), (0, end_min)]


def _periods_overlap_with_different_labels(rule: EffectiveRule) -> tuple[str, str] | None:
    """规则内部出现不同标签的时段重叠时返回两个标签，否则返回 None。

    同一标签的重叠/连续时段是允许的，会在核查时合并为一个有效时段。
    """
    periods = rule.periods
    for i, first in enumerate(periods):
        for second in periods[i + 1:]:
            if first.key == second.key:
                continue
            for a_start, a_end in _segments(first):
                for b_start, b_end in _segments(second):
                    if max(a_start, b_start) < min(a_end, b_end):
                        return first.label, second.label
    return None


def _uncovered_minutes(rule: EffectiveRule) -> str | None:
    """规则未覆盖全天时返回一个简短描述，否则返回 None。

    未覆盖的钟点没有峰谷归属，落在其中的采样区间只能待核定。
    """
    covered: set[int] = set()
    for period in rule.periods:
        for start_min, end_min in _segments(period):
            covered.update(range(start_min, end_min))
    if len(covered) == 24 * 60:
        return None
    missing = [minute for minute in range(24 * 60) if minute not in covered]
    if len(missing) == 1:
        text = f"{missing[0] // 60:02d}:{missing[0] % 60:02d}"
    else:
        text = f"{missing[0] // 60:02d}:{missing[0] % 60:02d}-{missing[-1] // 60:02d}:{missing[-1] % 60:02d}"
    return text


class RuleSet:
    def __init__(self, rules: list[EffectiveRule]):
        self.rules = list(rules)

    def resolve_date(self, day: date) -> _DateResolution:
        active = [rule for rule in self.rules if rule.applies_on(day)]
        if not active:
            return _DateResolution(
                status="missing",
                detail=f"当地日期 {day.isoformat()} 没有生效的峰谷时段规则",
            )

        signatures = {rule.signature for rule in active}
        if len(signatures) > 1:
            ids = "、".join(rule.rule_id for rule in active)
            return _DateResolution(
                status="conflict",
                rules=tuple(active),
                detail=f"当地日期 {day.isoformat()} 同时生效的规则 {ids} 时段定义不一致",
            )

        rule = active[0]
        overlap = _periods_overlap_with_different_labels(rule)
        if overlap is not None:
            return _DateResolution(
                status="conflict",
                rule=rule,
                rules=(rule,),
                detail=(
                    f"规则 {rule.rule_id} 内标签“{overlap[0]}”与“{overlap[1]}”的时段重叠，"
                    "无法唯一确定有效时段"
                ),
            )
        gap = _uncovered_minutes(rule)
        if gap is not None:
            return _DateResolution(
                status="gap",
                rule=rule,
                rules=(rule,),
                detail=f"规则 {rule.rule_id} 未覆盖全天，{gap} 无任何生效时段",
            )
        return _DateResolution(status="ok", rule=rule, rules=(rule,))

    def judge(self, record_start: datetime, record_end: datetime, original_label: str) -> Decision:
        if record_start >= record_end:
            return PendingDecision(
                PendingReason.INVALID_INTERVAL,
                "起止时间无效：起始时间必须早于结束时间",
            )
        if record_end - record_start > MAX_RECORD_SPAN:
            return PendingDecision(
                PendingReason.INVALID_INTERVAL,
                "单条记录采样跨度超过 24 小时，不是可核查的 15 分钟明细",
            )
        if record_start.tzinfo is not None or record_end.tzinfo is not None:
            return PendingDecision(
                PendingReason.INVALID_INTERVAL,
                "起止时间必须为不带时区偏移的当地本地时间",
            )

        first_day = record_start.date()
        last_day = (record_end - ONE_MICROSECOND).date()
        involved_days = [first_day + timedelta(days=offset)
                         for offset in range((last_day - first_day).days + 1)]

        # 规则冲突/缺失按“记录涉及的当地日期”判定，即使采样时刻看起来明确。
        for day in involved_days:
            resolution = self.resolve_date(day)
            if resolution.status == "conflict":
                return PendingDecision(PendingReason.RULE_CONFLICT, resolution.detail)
            if resolution.status == "missing":
                return PendingDecision(PendingReason.MISSING_RULE, resolution.detail)

        intervals = self._build_intervals(first_day - timedelta(days=1), last_day)
        overlapping = [
            interval for interval in intervals
            if interval.start < record_end and interval.end > record_start
        ]
        # 不同标签的覆盖区间在采样区间内重叠：规则本身冲突，不能判定。
        for i, first in enumerate(overlapping):
            for second in overlapping[i + 1:]:
                if first.label_key == second.label_key:
                    continue
                overlap_start = max(first.start, second.start)
                overlap_end = min(first.end, second.end)
                if overlap_start < overlap_end:
                    return PendingDecision(
                        PendingReason.RULE_CONFLICT,
                        f"采样区间内标签“{first.label_display}”与“{second.label_display}”"
                        "的生效时段重叠，无法唯一确定有效时段",
                    )

        covering = [
            interval for interval in intervals
            if interval.start <= record_start and interval.end >= record_end
        ]
        covering_labels = {interval.label_key for interval in covering}
        if len(covering_labels) == 1:
            chosen = covering[0]
            return JudgedDecision(
                expected_label=chosen.label_display,
                expected_label_key=chosen.label_key,
                valid_period_start=chosen.start,
                valid_period_end=chosen.end,
                source_rule_ids=tuple(sorted(chosen.rule_ids)),
                is_mislabeled=normalize_label(original_label) != chosen.label_key,
            )
        if len(covering_labels) > 1:
            return PendingDecision(
                PendingReason.RULE_CONFLICT,
                "采样区间同时完整落在多个不同标签的生效时段内，无法唯一确定有效时段",
            )

        intersecting_labels = {interval.label_key for interval in overlapping}
        if len(intersecting_labels) >= 2:
            labels = "、".join(sorted({
                interval.label_display for interval in overlapping
            }))
            return PendingDecision(
                PendingReason.CROSS_PERIOD,
                f"采样区间横跨“{labels}”等多个时段，无法在不拆分电量的前提下核定标签",
            )
        if len(intersecting_labels) == 1:
            label = overlapping[0].label_display
            return PendingDecision(
                PendingReason.MISSING_PERIOD,
                f"采样区间只有部分落在“{label}”时段内，其余时间无生效时段覆盖，"
                "无法在不拆分电量的前提下核定标签",
            )
        return PendingDecision(
            PendingReason.MISSING_PERIOD,
            "采样区间没有任何生效时段覆盖，无法核定标签",
        )

    def _build_intervals(self, first_day: date, last_day: date) -> list[_LabelInterval]:
        raw: list[_LabelInterval] = []
        day = first_day
        while day <= last_day:
            resolution = self.resolve_date(day)
            if resolution.status != "conflict" and resolution.rule is not None:
                for period in resolution.rule.periods:
                    start = datetime.combine(day, period.start)
                    if period.start < period.end:
                        end = datetime.combine(day, period.end)
                        spans = [(start, end)]
                    else:
                        # 跨午夜：22:00-06:00 既覆盖当天 22:00 至次日 06:00，
                        # 也覆盖当天 00:00 至 06:00（同一当地日期规则下的整日时段表）。
                        spans = [
                            (start, datetime.combine(day + timedelta(days=1), period.end)),
                            (datetime.combine(day, datetime.min.time()), datetime.combine(day, period.end)),
                        ]
                    for span_start, span_end in spans:
                        raw.append(_LabelInterval(
                            start=span_start,
                            end=span_end,
                            label_key=period.key,
                            label_display=period.label,
                            rule_ids={resolution.rule.rule_id},
                        ))
            # 规则缺失或冲突时不会产生时段；相关记录已在 judge() 中拦截。
            day += timedelta(days=1)

        raw.sort(key=lambda interval: (interval.start, interval.end))
        merged: list[_LabelInterval] = []
        for interval in raw:
            last = merged[-1] if merged else None
            if (
                last is not None
                and last.label_key == interval.label_key
                and interval.start <= last.end  # 重叠或恰好连续
            ):
                if interval.end > last.end:
                    last.end = interval.end
                last.rule_ids |= interval.rule_ids
                if last.label_display != interval.label_display:
                    # 同一比较键的不同写法：保留先出现的展示标签。
                    pass
                continue
            merged.append(_LabelInterval(
                start=interval.start,
                end=interval.end,
                label_key=interval.label_key,
                label_display=interval.label_display,
                rule_ids=set(interval.rule_ids),
            ))
        return merged


def _kwh(value: Decimal) -> Decimal:
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class Verdict:
    """一条记录的核查结果，record 为抓取层记录。"""

    record: object
    decision: Decision

    @property
    def is_pending(self) -> bool:
        return isinstance(self.decision, PendingDecision)

    @property
    def is_mislabeled(self) -> bool:
        return isinstance(self.decision, JudgedDecision) and self.decision.is_mislabeled


@dataclass
class VerificationOutcome:
    verdicts: list[Verdict]

    @property
    def judged(self) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if not verdict.is_pending]

    @property
    def pending(self) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.is_pending]

    @property
    def mislabeled(self) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.is_mislabeled]

    @property
    def correct(self) -> list[Verdict]:
        return [verdict for verdict in self.judged if not verdict.is_mislabeled]

    def _sum(self, verdicts: list[Verdict]) -> Decimal:
        return _kwh(sum((verdict.record.energy_kwh for verdict in verdicts), Decimal("0")))

    @property
    def verifiable_kwh(self) -> Decimal:
        return self._sum(self.judged)

    @property
    def correct_kwh(self) -> Decimal:
        return self._sum(self.correct)

    @property
    def mislabeled_kwh(self) -> Decimal:
        return self._sum(self.mislabeled)

    @property
    def pending_kwh(self) -> Decimal:
        return self._sum(self.pending)

    @property
    def known_total_kwh(self) -> Decimal:
        return _kwh(self.verifiable_kwh + self.pending_kwh)


def verify_records(records: list, rules: list[EffectiveRule]) -> VerificationOutcome:
    ruleset = RuleSet(rules)
    verdicts = [
        Verdict(record=record, decision=ruleset.judge(record.start, record.end, record.original_label))
        for record in records
    ]
    return VerificationOutcome(verdicts=verdicts)
