"""供电商用电明细抓取 / 解析测试。

直接以 ``file://`` 协议读取随仓库提供的 ``quote-site/usage`` 静态样例，
复现错标、跨时段、跨午夜、缺规则、规则冲突与抓取失败等场景。

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import unittest

from app.verification.label_check import (
    EffectiveRule,
    PendingReason,
    RulePeriod,
    RuleSet,
    normalize_label,
    verify_records,
)
from app.verification.usage_scraper import UsageRecord, crawl_usage_site, parse_usage_list_page

ROOT = Path(__file__).resolve().parent.parent
USAGE_URL = (ROOT / "quote-site" / "usage" / "index.html").as_uri()
DEMO_REQUEST = ROOT / "demo" / "verify_request.json"


def load_rules() -> list[EffectiveRule]:
    payload = json.loads(DEMO_REQUEST.read_text(encoding="utf-8"))
    rules = []
    for rule in payload["rules"]:
        rules.append(
            EffectiveRule(
                rule_id=rule["rule_id"],
                effective_from=date.fromisoformat(rule["effective_from"]),
                effective_to=date.fromisoformat(rule["effective_to"]) if rule["effective_to"] else None,
                periods=tuple(
                    RulePeriod(
                        label=period["label"],
                        start=time.fromisoformat(period["start"]),
                        end=time.fromisoformat(period["end"]),
                    )
                    for period in rule["periods"]
                ),
            )
        )
    return rules


def make_record(
    start: str,
    end: str,
    energy: str,
    label: str,
    detail_id: str = "T001",
    local_date: str | None = None,
):
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    return UsageRecord(
        detail_id=detail_id,
        local_date=local_date or start_dt.date().isoformat(),
        start=start_dt,
        end=end_dt,
        energy_kwh=Decimal(energy),
        original_label=label,
        detail_url=f"file:///demo/{detail_id}.html",
        list_url="file:///demo/index.html",
    )


NORMAL_RULE = EffectiveRule(
    rule_id="R-TEST",
    effective_from=date(2026, 11, 1),
    effective_to=None,
    periods=(
        RulePeriod(label="谷", start=time(22, 0),
                   end=time(6, 0)),
        RulePeriod(label="平", start=time(6, 0),
                   end=time(8, 0)),
        RulePeriod(label="峰", start=time(8, 0),
                   end=time(11, 0)),
        RulePeriod(label="平", start=time(11, 0),
                   end=time(18, 0)),
        RulePeriod(label="峰", start=time(18, 0),
                   end=time(22, 0)),
    ),
)


class CrawlTest(unittest.TestCase):
    def test_follows_pagination_and_lists_failed_detail(self) -> None:
        result = crawl_usage_site(USAGE_URL)
        self.assertEqual(len(result.visited_list_urls), 2)
        self.assertEqual([d.detail_id for d in result.details], ["D001", "D002", "D003", "D004", "D005"])
        self.assertEqual(len(result.failures), 1)
        failure = result.failures[0]
        self.assertEqual(failure.stage, "detail")
        self.assertEqual(failure.status, 404)
        self.assertTrue(failure.url.endswith("d006.html"))

    def test_parses_interval_energy_label_and_source(self) -> None:
        result = crawl_usage_site(USAGE_URL)
        d001 = next(d for d in result.details if d.detail_id == "D001")
        self.assertEqual(d001.local_date, "2026-11-03")
        self.assertEqual(len(d001.records), 95)  # 96 个 15 分钟槽，其中两条合并为跨时段记录
        first = d001.records[0]
        self.assertEqual(first.start, datetime(2026, 11, 3, 0, 0))
        self.assertEqual(first.end, datetime(2026, 11, 3, 0, 15))
        self.assertEqual(first.energy_kwh, Decimal("0.25"))
        self.assertEqual(first.original_label, "谷")
        self.assertIsNotNone(first.source_range_start)
        self.assertTrue(first.source_range_start.endswith("#r-001"))
        self.assertTrue(first.detail_url.endswith("d001.html"))

    def test_second_list_page_entries(self) -> None:
        empty_url = (ROOT / "quote-site" / "usage" / "index-2.html").as_uri()
        # index-2 也是列表页，会抓取其明细；这里只验证列表解析器可读取条目。
        entries, next_url = parse_usage_list_page(
            (ROOT / "quote-site" / "usage" / "index-2.html").read_text(encoding="utf-8"),
            empty_url,
        )
        self.assertEqual([e.detail_id for e in entries], ["D004", "D005", "D006"])
        self.assertIsNone(next_url)


class LabelJudgementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ruleset = RuleSet([NORMAL_RULE])

    def test_correct_label_within_single_period_is_judged(self) -> None:
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 9, 30), datetime(2026, 11, 3, 9, 45), "峰"
        )
        self.assertFalse(decision.is_mislabeled)
        self.assertEqual(decision.expected_label, "峰")

    def test_mislabeled_record_is_reported(self) -> None:
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 9, 30), datetime(2026, 11, 3, 9, 45), "谷"
        )
        self.assertTrue(decision.is_mislabeled)
        self.assertEqual(decision.expected_label, "峰")

    def test_label_comparison_ignores_width_and_case(self) -> None:
        self.assertEqual(normalize_label(" 峰 "), normalize_label("峰"))
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 9, 30), datetime(2026, 11, 3, 9, 45), "  峰 "
        )
        self.assertFalse(decision.is_mislabeled)

    def test_interval_crossing_two_periods_is_pending(self) -> None:
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 10, 45), datetime(2026, 11, 3, 11, 15), "峰"
        )
        self.assertEqual(decision.reason, PendingReason.CROSS_PERIOD)

    def test_interval_exactly_on_boundary_fits_single_period(self) -> None:
        # [11:00, 11:15) 恰好在“峰”结束后开始，完整落在“平”内。
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 11, 0), datetime(2026, 11, 3, 11, 15), "平"
        )
        self.assertFalse(decision.is_mislabeled)
        self.assertEqual(decision.expected_label, "平")

    def test_interval_fully_inside_cross_midnight_valley_is_judged(self) -> None:
        decision = self.ruleset.judge(
            datetime(2026, 11, 4, 23, 50), datetime(2026, 11, 5, 0, 5), "谷"
        )
        self.assertFalse(decision.is_mislabeled)
        self.assertEqual(decision.expected_label, "谷")
        self.assertEqual(decision.valid_period_start, datetime(2026, 11, 4, 22, 0))
        self.assertEqual(decision.valid_period_end, datetime(2026, 11, 5, 6, 0))

    def test_interval_crossing_midnight_boundary_is_pending(self) -> None:
        # 05:45-06:15 横跨谷/平，不得判定。
        decision = self.ruleset.judge(
            datetime(2026, 11, 4, 5, 45), datetime(2026, 11, 4, 6, 15), "谷"
        )
        self.assertEqual(decision.reason, PendingReason.CROSS_PERIOD)

    def test_missing_rule_for_local_date_is_pending(self) -> None:
        ruleset = RuleSet([EffectiveRule(
            rule_id="R-EXPIRED",
            effective_from=date(2026, 11, 1),
            effective_to=date(2026, 11, 30),
            periods=NORMAL_RULE.periods,
        )])
        decision = ruleset.judge(
            datetime(2026, 12, 1, 9, 0), datetime(2026, 12, 1, 9, 15), "峰"
        )
        self.assertEqual(decision.reason, PendingReason.MISSING_RULE)

    def test_conflicting_rules_make_records_pending(self) -> None:
        conflicting = EffectiveRule(
            rule_id="R-ALT",
            effective_from=date(2026, 11, 1),
            effective_to=None,
            periods=(RulePeriod(label="峰", start=time(8, 0),
                                end=time(20, 0)),),
        )
        ruleset = RuleSet([NORMAL_RULE, conflicting])
        decision = ruleset.judge(
            datetime(2026, 11, 3, 9, 0), datetime(2026, 11, 3, 9, 15), "峰"
        )
        self.assertEqual(decision.reason, PendingReason.RULE_CONFLICT)

    def test_rule_with_overlapping_different_labels_is_conflict(self) -> None:
        overlapping = EffectiveRule(
            rule_id="R-OV",
            effective_from=date(2026, 11, 1),
            effective_to=None,
            periods=(
                RulePeriod(label="峰", start=time(8, 0),
                           end=time(12, 0)),
                RulePeriod(label="平", start=time(11, 0),
                           end=time(18, 0)),
                RulePeriod(label="谷", start=time(22, 0),
                           end=time(6, 0)),
            ),
        )
        ruleset = RuleSet([overlapping])
        decision = ruleset.judge(
            datetime(2026, 11, 3, 9, 0), datetime(2026, 11, 3, 9, 15), "峰"
        )
        self.assertEqual(decision.reason, PendingReason.RULE_CONFLICT)

    def test_invalid_interval_is_pending(self) -> None:
        decision = self.ruleset.judge(
            datetime(2026, 11, 3, 9, 15), datetime(2026, 11, 3, 9, 15), "峰"
        )
        self.assertEqual(decision.reason, PendingReason.INVALID_INTERVAL)


class VerificationTotalsTest(unittest.TestCase):
    def test_energy_totals_do_not_split_pending_records(self) -> None:
        records = [
            make_record("2026-11-03T09:00", "2026-11-03T09:15", "0.25", "峰"),
            make_record("2026-11-03T09:15", "2026-11-03T09:30", "0.25", "谷"),  # 错标
            make_record("2026-11-03T10:45", "2026-11-03T11:15", "0.50", "峰"),  # 跨时段
        ]
        outcome = verify_records(records, [NORMAL_RULE])
        self.assertEqual(outcome.verifiable_kwh, Decimal("0.5"))
        self.assertEqual(outcome.correct_kwh, Decimal("0.25"))
        self.assertEqual(outcome.mislabeled_kwh, Decimal("0.25"))
        self.assertEqual(outcome.pending_kwh, Decimal("0.5"))
        self.assertEqual(outcome.known_total_kwh, Decimal("1.0"))

    def test_demo_site_end_to_end(self) -> None:
        crawl = crawl_usage_site(USAGE_URL)
        outcome = verify_records(crawl.records, load_rules())
        self.assertEqual(len(outcome.mislabeled), 2)
        mislabeled_labels = {v.decision.expected_label for v in outcome.mislabeled}
        self.assertEqual(mislabeled_labels, {"峰", "谷"})
        reasons = {v.decision.reason for v in outcome.pending}
        self.assertEqual(reasons, {PendingReason.CROSS_PERIOD, PendingReason.RULE_CONFLICT})
        self.assertEqual(outcome.correct_kwh, Decimal("94.7500"))
        self.assertEqual(outcome.mislabeled_kwh, Decimal("0.5000"))
        self.assertEqual(outcome.pending_kwh, Decimal("24.5000"))
        self.assertEqual(outcome.known_total_kwh, Decimal("119.7500"))


if __name__ == "__main__":
    unittest.main()
