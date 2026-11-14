# 公共采购公告采集系统

面向招标数据员的公告采集交付件：FastAPI 采集器 + 带限流/延迟/故障开关的本地模拟采购站，
Docker Compose 一键启动，浏览器即可启动采集、查看实时进度、停止任务、**补采缺页**并下载 JSON。

## 目录结构

```
.
├── docker-compose.yml          # 两服务编排：procurement-site + collector
├── collector/                  # 采集器（FastAPI + httpx + BeautifulSoup）
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # API、补采端点、JSON 下载、采购站同源代理、静态页挂载
│       ├── crawler.py          # 采集引擎：并发闸门、429 全站冷却、停止回收、补采缺页
│       ├── parser.py           # BeautifulSoup 列表/详情解析（失败抛 ParseError）
│       └── static/index.html   # 浏览器控制台（单文件，原生 JS）
├── site/                       # 本地模拟公共采购站
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # 栏目/详情页、限流、延迟、列表故障/坏详情恢复开关、管理页
│       └── data.py             # 确定性内存数据集（含跨栏目重复编号与 1 个坏详情页）
└── tests/                      # 契约测试与解析器单测（标准库 + venv 依赖）
```

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 采集控制台 | http://localhost:8000/ | 启动/停止/补采/进度/事件/下载 JSON |
| 采购站（同源代理入口） | http://localhost:8000/site/ | 浏览器入口，经采集器反代到站点 |
| 采购站开关管理 | http://localhost:8000/site/admin | 限流、延迟、列表页暂时故障、坏详情恢复开关 |
| 采购站直连 | http://localhost:8080/ | 仅排障时使用 |

## 采集规则与实现对照

| 需求 | 实现 |
| --- | --- |
| 固定站点两个栏目，遍历分页列表及详情 | `Crawler` 配置 `/tenders`、`/awards`；首页解析 `data-total-pages` 后入队其余列表页，再逐条入队详情页 |
| 按公告编号去重 | 集合 `_seen_codes` 跨栏目去重；同编号只采一次详情，跳过计数在分栏目进度中可见 |
| 提取标题、采购单位、截止日期 | `parser.parse_detail_page`，缺任一字段即 `ParseError` |
| 可下载 JSON | `GET /api/records/download`，附件文件名带轮次 ID，含 `complete` 标记 |
| 单进程同时只采一轮 | 单 uvicorn worker + `round_lock`；重复启动返回 `409` |
| 最多两个请求在途 | 全站共享 `asyncio.Semaphore(2)`，两个栏目共用 |
| 429 按 Retry-After 整数秒全站暂停 | `_extend_cooldown` 维护单一冷却钟，`_pass_gate` 拦截所有后续请求（含另一栏目与重试）；后续 429 只延长不缩短；已在途请求允许收尾 |
| 每页最多尝试三次 | 列表/详情统一 `_get_parsed_page`，传输错误、4xx/5xx、解析失败均消耗尝试次数 |
| 耗尽/解析失败须列出未完成页，不能报整轮成功 | 失败页进入 `unfinished`；轮次结束状态为 `partial` 而非 `completed` |
| 停止阻止排队/冷却请求，关闭在途请求并回收连接后才显示已停止 | `stop_round` 先置停止位，再 `aclose()` 关闭在途请求与连接池，`await` 轮次协程收尾后状态才变 `stopped`，随后重建客户端允许新轮 |
| 旧响应不能写入新轮 | 每轮生成 `round_id`，冷却唤醒与回写前校验；新轮重建客户端、状态与记录容器 |

## 补采缺页（resume）

源站短时故障导致本轮只采齐部分公告时，不用重采整轮——对 `partial` 轮次点“补采缺页”即可：

| 需求 | 实现 |
| --- | --- |
| 仅对已结束的 partial 轮次及补采停止后的状态开放 | `POST /api/rounds/resume` 仅在 `status == "partial"` 时放行；补采失败或被停止后轮次回到 `partial`，可再次补采；其余状态（idle/running/stopped/completed）一律 `409` |
| 只重取未完成的列表页和详情页 | `start_resume` 仅从 `unfinished` 清单重建任务；已成功的页面零重发（契约测试逐 URL 校验请求计数） |
| 列表补齐后采录新发现且未成功的详情，按编号并入原轮 | 重取列表页（含第 1 页）成功后，补抓该栏目从未完成的列表页；新发现的详情若不在已有记录中则入队，成功记录按公告编号合并进原轮，轮次 ID 不变 |
| 补采期间不能启动新轮或重复补采 | 补采复用同一把 `round_lock`：补采中启动新轮或再次补采均 `409` |
| 下载结果仍须标为未完成 | 补采中状态为 `resuming`；下载 JSON 的 `complete` 仅在 `completed` 时为 `true` |
| 补采失败/被停止时已有公告保持可见，未完成页可继续补采 | 补采不清空记录；未完成清单由本次补采重建，失败/停止的页面重新入列 |
| 全部缺页及新发现详情成功后才显示 completed | 补采收尾时仅当 `unfinished` 为空才置 `completed`，否则回到 `partial` |
| 沿用两请求上限、429 全站冷却、停止回收 | 补采复用同一 `_inflight` 信号量、`_pass_gate` 冷却钟与 `stop_round` 回收流程 |

轮次状态机：`idle → running → completed | partial | stopped`（停止过程中短暂为 `stopping`）；
补采路径：`partial → resuming → completed | partial`（补采被停止或仍有缺页时回到 `partial`）。

## 模拟采购站说明

- 数据集：36 条招标公告（6 个列表页）+ 24 条中标公告（4 个列表页），
  中标公告复用前 24 个招标编号，用于验证跨栏目去重；
- `ZB-2026-010` 详情页故意缺少“采购单位/截止日期”，用于验证三次解析失败后
  进入未完成页面列表、整轮标记为 `partial`；
- 限流为滑动窗口，默认 10 秒窗口 12 个请求，超限返回 `429` 与整数 `Retry-After: 5`；
- `/admin` 可运行时切换：
  - 限流开关、窗口容量、窗口秒数与每请求延迟毫秒（保存即重置计数，也可单独重置）；
  - **列表页暂时故障**：指定栏目 + 页码返回 `503`，模拟源站短时故障，关闭开关即恢复；
  - **坏详情恢复**：勾选后 `ZB-2026-010` 补齐“采购单位/截止日期”，模拟源站修复。

## 推荐演示流程

1. `docker compose up --build`，打开 http://localhost:8000/ ；
2. 直接点“启动一轮采集”：观察到 `partial`——35 条记录、1 个坏详情页在未完成列表中；
3. 在 `/site/admin` 勾选“坏详情恢复”，回到控制台点“补采缺页”：只重取该详情页，
   整轮变为 `completed`、36 条记录，轮次 ID 不变；
4. 在 `/site/admin` 勾选“列表页暂时故障”（如招标栏目第 3 页）后启动新一轮：
   该页 503 三次尝试后进入未完成，整轮 `partial`；下载 JSON 可见 `complete: false`；
5. 不关闭故障直接“补采缺页”：仍 `partial`，已有 35 条记录保持可见；
   关闭故障开关再补采：列表页补齐，新发现的详情按编号并入，状态变 `completed`；
6. 在 `/site/admin` 把窗口容量调到 3：再启动一轮，观察控制台“全站冷却剩余”倒计时，
   冷却期间两个栏目均无新请求完成；
7. 冷却进行中点“停止”：状态在 1 秒内变为 `stopped`，排队/冷却中的页面进入未完成列表；
8. 立即再启动一轮：状态与记录完全重置，证明旧轮响应不会写入新轮；
9. 点“下载 JSON”获取结构化结果（含未完成页清单与 `complete` 标记）。

## 本地测试（无需 Docker）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r collector/requirements.txt
.venv/bin/python -I tests/test_parser.py
.venv/bin/python -I tests/test_crawler_contract.py
.venv/bin/python -I tests/test_resume_contract.py
.venv/bin/python -I tests/test_download_api.py
```

契约测试用内存假上游验证：在途请求峰值恰为 2、429 后整站等待不少于
Retry-After 且重试可恢复、冷却中停止后零请求泄漏、停止后新轮干净无旧数据；
补采契约测试验证：只重取未完成页（逐 URL 请求计数）、列表整体补齐与新详情发现、
按编号并入原轮且轮次 ID 不变、补采中禁止新轮/重复补采、补采停止后记录保持可见且可再补采、
全部缺页成功后才显示 `completed`；下载 API 测试验证非 `completed` 状态一律 `complete: false`
及补采端点的 `409` 映射。
