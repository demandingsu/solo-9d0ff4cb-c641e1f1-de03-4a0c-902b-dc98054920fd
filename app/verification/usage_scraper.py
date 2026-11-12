"""供电商用电明细站抓取与解析：分页列表 + 逐日明细分页。

页面结构随仓库提供（见 ../quote-site/usage），解析器依赖稳定数据属性：
- 列表页：``main[data-page=usage-list]``，条目为 ``article.usage-card[data-detail-id]``，
  内含 ``a.detail-link``；分页为 ``a[rel=next]``
- 明细页：``main[data-page=usage-detail]``，数据行
  ``tr[data-interval]`` 携带 ``data-interval-start`` / ``data-interval-end``（当地本地时间，
  ISO 8601）、``data-energy-kwh`` 与 ``data-period-label``。

结构不完整的数据行不会被静默跳过，而是令整页解析失败并记录详情地址，
避免遗漏记录后给出不完整的核查结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..fetcher import FetchError, fetch_url, join_url
from ..html_dom import parse_html

MAX_LIST_PAGES = 50


@dataclass
class UsageRecord:
    detail_id: str
    local_date: str
    start: datetime
    end: datetime
    energy_kwh: Decimal
    original_label: str
    detail_url: str
    list_url: str
    source_range_start: str | None = None
    source_range_end: str | None = None


@dataclass
class UsageDetail:
    detail_id: str
    local_date: str
    detail_url: str
    list_url: str
    records: list[UsageRecord]


@dataclass
class UsageListEntry:
    detail_id: str
    detail_url: str
    list_url: str


@dataclass
class UsageFailure:
    url: str
    stage: str  # list | detail
    reason: str
    status: int | None = None


@dataclass
class UsageCrawlResult:
    details: list[UsageDetail] = field(default_factory=list)
    failures: list[UsageFailure] = field(default_factory=list)
    visited_list_urls: list[str] = field(default_factory=list)
    empty_detail_urls: list[str] = field(default_factory=list)

    @property
    def records(self) -> list[UsageRecord]:
        return [record for detail in self.details for record in detail.records]


def _parse_local_datetime(value: str | None, what: str) -> datetime:
    if not value:
        raise ValueError(f"{what}缺失")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{what}无法解析为当地时间：{value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{what}带时区偏移，无法按当地日期核查：{value!r}")
    return parsed


def _parse_energy(value: str | None) -> Decimal:
    if not value:
        raise ValueError("用电量缺失")
    try:
        energy = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"用电量无法解析：{value!r}") from exc
    if not energy.is_finite():
        raise ValueError(f"用电量不是有限数值：{value!r}")
    if energy < 0:
        raise ValueError(f"用电量为负数：{value!r}")
    return energy


def parse_usage_list_page(markup: str, page_url: str) -> tuple[list[UsageListEntry], str | None]:
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="usage-list")
    if main is None:
        raise ValueError("列表页缺少 usage-list 主容器")

    entries: list[UsageListEntry] = []
    for card in main.find_all(tag="article", attr="data-detail-id"):
        detail_id = (card.get("data-detail-id") or "").strip()
        link = card.find(tag="a", attr="class", attr_value="detail-link")
        href = link.get("href") if link is not None else None
        if not detail_id:
            raise ValueError("列表条目缺少 data-detail-id")
        if not href:
            raise ValueError(f"列表条目 {detail_id} 缺少 detail-link 地址")
        entries.append(
            UsageListEntry(
                detail_id=detail_id,
                detail_url=join_url(page_url, href),
                list_url=page_url,
            )
        )

    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = join_url(page_url, next_link.get("href")) if next_link is not None and next_link.get("href") else None
    return entries, next_url


def parse_usage_detail_page(markup: str, detail_url: str, list_url: str) -> UsageDetail:
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="usage-detail")
    if main is None:
        raise ValueError("明细页缺少 usage-detail 主容器")

    detail_id = (main.get("data-detail-id") or "").strip()
    if not detail_id:
        raise ValueError("明细页缺少 data-detail-id")
    local_date = (main.get("data-usage-date") or "").strip()
    if not local_date:
        raise ValueError("明细页缺少 data-usage-date")

    records: list[UsageRecord] = []
    for index, row in enumerate(main.find_all(tag="tr", attr="data-interval"), start=1):
        try:
            start = _parse_local_datetime(row.get("data-interval-start"), "起始时间")
            end = _parse_local_datetime(row.get("data-interval-end"), "结束时间")
            energy = _parse_energy(row.get("data-energy-kwh"))
            label = (row.get("data-period-label") or "").strip()
            if not label:
                raise ValueError("原标签缺失")
            range_start = (row.get("data-range-start") or "").strip()
            range_end = (row.get("data-range-end") or "").strip()
            has_range_start = bool(range_start)
            has_range_end = bool(range_end)
            if has_range_start != has_range_end:
                raise ValueError("详情来源范围缺少起始或结束地址")
            source_range_start = join_url(detail_url, range_start) if has_range_start else None
            source_range_end = join_url(detail_url, range_end) if has_range_end else None
        except ValueError as exc:
            raise ValueError(f"明细第 {index} 行数据结构不完整：{exc}") from exc
        records.append(
            UsageRecord(
                detail_id=detail_id,
                local_date=local_date,
                start=start,
                end=end,
                energy_kwh=energy,
                original_label=label,
                detail_url=detail_url,
                list_url=list_url,
                source_range_start=source_range_start,
                source_range_end=source_range_end,
            )
        )

    return UsageDetail(
        detail_id=detail_id,
        local_date=local_date,
        detail_url=detail_url,
        list_url=list_url,
        records=records,
    )


def crawl_usage_site(list_url: str, timeout: float = 8.0) -> UsageCrawlResult:
    """从明细首页开始，跟随分页并抓取每个明细页。"""
    result = UsageCrawlResult()
    seen_list_urls: set[str] = set()
    entries: list[UsageListEntry] = []
    current: str | None = list_url

    while current and current not in seen_list_urls and len(seen_list_urls) < MAX_LIST_PAGES:
        seen_list_urls.add(current)
        result.visited_list_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.failures.append(UsageFailure(url=current, stage="list", reason=exc.reason, status=exc.status))
            break
        try:
            page_entries, next_url = parse_usage_list_page(page.text, page.url)
        except ValueError as exc:
            result.failures.append(UsageFailure(url=current, stage="list", reason=str(exc), status=page.status))
            break
        entries.extend(page_entries)
        current = next_url

    seen_detail: set[str] = set()
    for entry in entries:
        if entry.detail_url in seen_detail:
            continue
        seen_detail.add(entry.detail_url)
        try:
            page = fetch_url(entry.detail_url, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                UsageFailure(url=entry.detail_url, stage="detail", reason=exc.reason, status=exc.status)
            )
            continue
        try:
            detail = parse_usage_detail_page(page.text, page.url, entry.list_url)
        except ValueError as exc:
            result.failures.append(
                UsageFailure(url=entry.detail_url, stage="detail", reason=str(exc), status=page.status)
            )
            continue
        if not detail.records:
            result.empty_detail_urls.append(entry.detail_url)
        result.details.append(detail)

    return result
