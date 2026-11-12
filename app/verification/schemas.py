"""峰谷标签核查 API 的请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class RulePeriodIn(BaseModel):
    label: str = Field(min_length=1, description="时段标签，如“峰”“平”“谷”")
    start: time = Field(description="当地本地时间起始，含")
    end: time = Field(description="当地本地时间结束，不含；小于起始时间表示跨午夜")

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("时段标签不能为空")
        return value

    @field_validator("start", "end")
    @classmethod
    def _drop_timezone(cls, value: time) -> time:
        # 时段规则按当地钟表时间生效，不接受时区偏移写法。
        return value.replace(tzinfo=None)

    @model_validator(mode="after")
    def _non_empty_period(self) -> "RulePeriodIn":
        if self.start == self.end:
            raise ValueError(f"时段“{self.label}”起止时间相同，零长度时段无效")
        return self


class EffectiveRuleIn(BaseModel):
    rule_id: str = Field(min_length=1, description="规则标识，用于报告追溯")
    effective_from: date = Field(description="规则生效起始当地日期（含）")
    effective_to: date | None = Field(default=None, description="规则生效截止当地日期（含），为空表示长期有效")
    periods: list[RulePeriodIn] = Field(min_length=1, description="该规则定义的峰谷时段")

    @field_validator("rule_id")
    @classmethod
    def _strip_rule_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("rule_id 不能为空")
        return value

    @model_validator(mode="after")
    def _check_dates(self) -> "EffectiveRuleIn":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(f"规则 {self.rule_id} 的生效截止日期早于起始日期")
        return self


class VerifyLabelsRequest(BaseModel):
    list_url: str = Field(min_length=1, description="供电商用电明细首页 URL，支持 http/https/file")
    rules: list[EffectiveRuleIn] = Field(min_length=1, description="按当地日期生效的峰谷时段规则")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @field_validator("list_url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("list_url 仅支持 http://、https:// 或 file:// 协议")
        return value


class RecordSourceOut(BaseModel):
    detail_id: str
    detail_url: str
    list_url: str
    source_range_start: str | None
    source_range_end: str | None


class RuleRefOut(BaseModel):
    rule_id: str
    effective_from: date
    effective_to: date | None


class UsageRecordOut(BaseModel):
    detail_id: str
    local_date: str
    interval_start: datetime
    interval_end: datetime
    energy_kwh: float
    original_label: str
    expected_label: str | None
    status: Literal["correct", "mislabeled", "pending"]
    pending_reason: str | None
    pending_detail: str | None
    valid_period_start: datetime | None
    valid_period_end: datetime | None
    source_rules: list[RuleRefOut]
    source: RecordSourceOut


class EnergyTotalsOut(BaseModel):
    """电量汇总（kWh，四位小数）。待核定电量原样汇总，不做时间比例分摊。"""

    total_kwh: float = Field(description="已抓取到的全部记录电量合计")
    verifiable_kwh: float = Field(description="可核定电量：采样区间完整落在唯一有效时段内的记录")
    correct_kwh: float = Field(description="可核定且原标签正确的电量")
    mislabeled_kwh: float = Field(description="可核定且原标签错标的电量")
    pending_kwh: float = Field(description="待核定电量：原样汇总，未按时间比例拆分")


class FailureOut(BaseModel):
    url: str = Field(description="抓取失败的列表页或明细页地址")
    stage: Literal["list", "detail"]
    reason: str
    status: int | None


class VerificationReportOut(BaseModel):
    list_url: str
    verification_status: Literal["verified", "pending_verification"]
    note: str
    crawl: dict
    rules_applied: list[RuleRefOut]
    totals: EnergyTotalsOut
    mislabeled_records: list[UsageRecordOut]
    pending_records: list[UsageRecordOut]
    failures: list[FailureOut]
