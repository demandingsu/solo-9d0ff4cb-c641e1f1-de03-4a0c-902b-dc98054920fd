# 连锁便利店电价套餐比较与用电标签核查 API

两个业务场景共用同一 FastAPI 应用：

1. **套餐比较**：能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策。
2. **峰谷标签核查**：门店能源资料员核查供电商每 15 分钟公布的用电记录，找出峰谷标签错标的区间，
   形成可联系供电商更正的错标明细。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **抓取与计费核心**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal`），离线可测
- **本地演示站**：随仓库提供的纯静态站点（`quote-site/`），nginx 容器托管，无需外网
- **部署**：Docker Compose 一键启动 API + 演示站

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 应用与套餐比较路由
│   ├── schemas.py     # 套餐比较请求/响应 Pydantic 模型
│   ├── scraper.py     # 报价站分页列表 + 套餐详情抓取解析
│   ├── fetcher.py     # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py    # 标准库实现的轻量 HTML DOM
│   ├── billing.py     # 峰谷逐档计费、资格校验、年度汇总
│   └── verification/  # 峰谷标签核查子包
│       ├── router.py        # POST /api/v1/verify-labels
│       ├── usage_scraper.py # 明细分页列表 + 逐日明细抓取解析
│       ├── label_check.py   # 按当地日期生效的时段规则与保守判定引擎
│       └── schemas.py       # 核查请求/响应模型
├── quote-site/        # 本地演示站（静态 HTML，虚构演示数据）
│   ├── index.html / index-2.html   # 套餐列表第 1/2 页
│   ├── plans/p001..p006.html
│   ├── usage/         # 供电商用电明细站
│   │   ├── index.html / index-2.html   # 按日明细列表第 1/2 页
│   │   ├── d001..d005.html             # 每日 15 分钟明细样例
│   │   └── d006 故意缺失（404），演示详情抓取失败
│   ├── 404.html       # p007.html / d006.html 故意缺失
│   └── nginx.conf
├── tests/             # 标准库 unittest 离线测试
├── demo/request.json         # 套餐比较示例请求
├── demo/verify_request.json  # 标签核查示例请求（含跨午夜与规则冲突）
├── demo/run_demo.sh   # 启动本地演示站并调用两个 API
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 演示站：http://localhost:8080/index.html （明细站：http://localhost:8080/usage/index.html）
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

发起套餐比较请求（容器内通过 `http://quote-site/index.html` 访问演示站）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json
```

发起峰谷标签核查请求（示例请求中的 `list_url` 已指向 `http://quote-site/usage/index.html`）：

```bash
curl -X POST http://localhost:8000/api/v1/verify-labels \
  -H "Content-Type: application/json" \
  --data @demo/verify_request.json
```

## 无 Docker 的离线运行

核心抓取/计费/核查逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

手动启动演示站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
uvicorn app.main:app --host 0.0.0.0 --port 8000
bash demo/run_demo.sh          # 自动启动站点并依次调用两个 API
# 请求中也可用 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/usage/index.html"
```

## 请求格式（套餐比较）

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 请求格式（峰谷标签核查）

`POST /api/v1/verify-labels`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 供电商用电明细首页 URL，支持 `http(s)://` 与 `file://`；自动跟随分页 |
| `rules` | 按当地日期生效的峰谷时段规则列表，至少一条 |
| `rules[].rule_id` | 规则标识，报告中用于追溯 |
| `rules[].effective_from` / `effective_to` | 生效的当地日期区间（含端点）；`effective_to` 为空表示长期有效 |
| `rules[].periods[]` | 时段定义：`label` 标签、`start`/`end` 当地钟表时间；`end < start` 表示跨午夜 |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

示例（跨午夜“谷”时段 22:00-06:00，规则按当地日期切换）：

```json
{
  "list_url": "http://quote-site/usage/index.html",
  "rules": [
    {
      "rule_id": "R-2026-11-A",
      "effective_from": "2026-11-01",
      "effective_to": "2026-11-06",
      "periods": [
        {"label": "谷", "start": "22:00", "end": "06:00"},
        {"label": "平", "start": "06:00", "end": "08:00"},
        {"label": "峰", "start": "08:00", "end": "11:00"},
        {"label": "平", "start": "11:00", "end": "18:00"},
        {"label": "峰", "start": "18:00", "end": "22:00"}
      ]
    }
  ]
}
```

完整示例见 `demo/verify_request.json`。

## 标签核查规则（保守判定，不猜测分摊）

1. **按当地日期匹配规则**：记录的起止时间为不带时区偏移的当地本地时间；规则按记录涉及的
   每个当地日期分别解析。跨午夜规则（如 22:00-06:00）按“当天 22:00 至次日 06:00”生效，
   同一天 00:00-06:00 也归属该谷时段。
2. **仅当完整采样区间落在唯一有效时段内才判定**：`记录起 >= 时段起` 且 `记录止 <= 时段止`，
   且该区间只被一个标签覆盖，才判为“正确”或“错标”；时段端点采用 `[起, 止)`。
3. **列为待核定（不判对错）的情况**：
   - 采样区间跨多个时段（`cross_period`），例如一条记录横跨峰/平边界；
   - 记录涉及的当地日期没有任何生效规则（`missing_rule`）；
   - 生效规则未覆盖全天、采样区间部分落在无归属时间（`missing_period`）；
   - 同一当地日期同时生效多条时段定义不一致的规则，或规则内不同标签时段重叠（`rule_conflict`）；
   - 起止时间无效、带时区偏移或单条跨度超过 24 小时（`invalid_interval`）。
4. **电量绝不按时间比例拆分**：待核定记录的电量原样计入 `pending_kwh`；可核定记录计入
   `verifiable_kwh`，其中正确/错标分别计入 `correct_kwh` / `mislabeled_kwh`。
   `total_kwh = verifiable_kwh + pending_kwh`，四则拆分口径自洽可审计。
5. **同一标签、连续或重叠的时段合并为一个有效时段**（可跨越日期边界），因此整段位于谷时段的
   跨午夜记录可正常核定；规则内不同标签的时段重叠则视为冲突，不猜测优先级。

## 抓取失败处理

- 列表页（含分页）或明细 / 套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 标签核查中，只要存在任何抓取失败、存在空明细页、或存在待核定记录，
  整份报告 `verification_status` 标记为 `pending_verification`（待核定）；
  全部明细抓取成功且每条记录都可核定时为 `verified`。
- 比较 API 中，只要存在任何抓取失败，`conclusion.verification_status` 同样标记为
  `pending_verification`；全部成功且存在有效套餐时为 `verified`。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

套餐比较（`quote-site/`）：

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

标签核查（`quote-site/usage/`，规则见 `demo/verify_request.json`）：

| 明细页 | 当地日期 | 演示场景 |
| --- | --- | --- |
| D001 | 2026-11-03 | 22:00-22:15 原标“峰”应为“谷”、09:30-09:45 原标“谷”应为“峰” → 2 条错标；10:45-11:15 横跨峰/平 → 待核定 |
| D002 | 2026-11-04 | 23:50-00:05 跨午夜记录完整落在谷时段（22:00-次日06:00）→ 可核定正确 |
| D003 | 2026-11-05 | 该日无生效规则 → 整日待核定（`missing_rule`） |
| D004 | 2026-11-07 | 同时生效 R-B 与 R-C 两条冲突规则 → 整日待核定（`rule_conflict`） |
| D005 | 2026-11-08 | 仅一条规则生效，全部记录可核定且标签正确 |
| D006 | 2026-11-09 | 明细页故意 404 → 列入 `failures`，整份报告待核定 |

示例请求下：`records_crawled=478`，`mislabeled_count=2`，`pending_count=97`，
电量 `correct_kwh=94.75`、`mislabeled_kwh=0.50`、`pending_kwh=24.50`、`total_kwh=119.75`。

## 响应结构（标签核查，摘要）

```json
{
  "verification_status": "pending_verification",
  "note": "1 个列表页或明细页抓取失败；97 条记录无法唯一核定，整份报告待核定……",
  "crawl": { "details_crawled": 5, "records_crawled": 478, "mislabeled_count": 2, "pending_count": 97 },
  "totals": {
    "total_kwh": 119.75, "verifiable_kwh": 95.25, "correct_kwh": 94.75,
    "mislabeled_kwh": 0.5, "pending_kwh": 24.5
  },
  "mislabeled_records": [ {
    "detail_id": "D001",
    "interval_start": "2026-11-03T09:30:00",
    "interval_end": "2026-11-03T09:45:00",
    "energy_kwh": 0.25,
    "original_label": "谷",
    "expected_label": "峰",
    "status": "mislabeled",
    "valid_period_start": "2026-11-03T08:00:00",
    "valid_period_end": "2026-11-03T11:00:00",
    "source_rules": [ { "rule_id": "R-2026-11-A", "effective_from": "2026-11-01", "effective_to": "2026-11-06" } ],
    "source": { "detail_url": ".../d001.html", "list_url": ".../index.html",
                "source_range_start": ".../d001.html#r-039", "source_range_end": ".../d001.html#r-039" }
  } ],
  "pending_records": [ { "pending_reason": "cross_period", "pending_detail": "采样区间横跨“峰、平”等多个时段……" } ],
  "failures": [ { "url": ".../d006.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每条记录均带 `detail_id`、详情来源 `source.detail_url`、发现它的列表页
`source.list_url` 以及原始表格行锚点 `source_range_start/end`，便于资料员在联系供电商时
精确定位到具体区间。

## 套餐比较响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实"
  },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers", "detail": "..." } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（详情来源）与 `list_url`（发现该套餐的列表页），
月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。
