# 行业资讯模块 M1 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 行业资讯 M1:来源插件化采集 + telegraph/em_news 采集器 + DuckDB 落库 + 行业/个股关联 + REST API + /news 三 tab 页面。

**Architecture:** 复用 data_task 状态机模式(独立 news_task 表)、reader/writer 单写者、NewsSource 协议插件、前端仿 data/ 面板;关联层用词表匹配 + industry_classify 反查。

**Tech Stack:** Python / FastAPI / DuckDB / akshare / Next.js App Router / Vitest

**Spec:** docs/superpowers/specs/2026-09-10-industry-news-design.md

## Global Constraints

- 写走 writer(),读走 reader()(DuckDB 单写者)
- 新路由用 make_router(统一封套),main.py include_router 注册
- 内容截断 2000 字打 bit2;时间缺失用采集时刻兜底打 bit1;推断关联打 bit4
- news_id = sha1(source_name|external_id);INSERT OR IGNORE 去重
- 分页 limit≤200,keyword≤100 字符
- 任务状态机:pending→running→ok/partial/failed/interrupted;单来源失败不影响其他来源
- ruff:git commit --no-verify 绕全仓基线,新文件逐个 ruff check
- 测试:python -m pytest tests/unit/test_xxx.py -v;前端 cd web && npx vitest run

---

### Task 1: DDL + NewsItem 模型 + news store

**Files:**
- Create: `src/lquant/news/__init__.py`, `src/lquant/news/model.py`, `src/lquant/news/store.py`
- Test: `tests/unit/test_news_store.py`

**Interfaces(Produces,后续任务依赖):**
- `NewsItem`(frozen dataclass):source, source_name, external_id, title, content, url, symbols: tuple[str,...] = (), industry_code: str|None = None, published_at: datetime|None = None, collected_at: datetime|None = None, quality_flags: int = 0, source_tag: str = "";属性 `news_id`
- `init_news_ddl(con) -> None`
- `insert_news(con, items) -> int`(精确新增 = 插入前后 count 差)
- `query_news(con, source=None, industry=None, symbol=None, day=None, keyword=None, limit=50, offset=0) -> dict`(total + items;symbol 过滤用 DuckDB `list_contains`)
- `news_stats_by_industry(con) -> list[dict]`(industry_code/count)
- `news_stats_by_source(con) -> list[dict]`(source/source_name/count/last_collected_at)

- [ ] **Step 1 写失败测试** `tests/unit/test_news_store.py`:

```python
import duckdb
import pytest
from lquant.news.model import NewsItem
from lquant.news.store import (
    init_news_ddl, insert_news, query_news,
    news_stats_by_industry, news_stats_by_source,
)

@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_news_ddl(c)
    return c

def _item(**kw):
    base = dict(source="telegraph", source_name="cls", external_id="e1",
                title="t", content="c" * 10, url="u", symbols=(),
                industry_code=None, published_at=None, collected_at=None,
                quality_flags=0, source_tag="cls")
    base.update(kw)
    return NewsItem(**base)

def test_insert_dedupes(con):
    assert insert_news(con, [_item(), _item()]) == 1
    assert insert_news(con, [_item(external_id="e2")]) == 1
    assert query_news(con)["total"] == 2

def test_query_filters(con):
    insert_news(con, [
        _item(external_id="a", symbols=("000001.SZ",)),
        _item(external_id="b", industry_code="801010"),
        _item(external_id="c", content="锂矿价格上涨"),
    ])
    assert query_news(con, symbol="000001.SZ")["total"] == 1
    assert query_news(con, industry="801010")["total"] == 1
    assert query_news(con, keyword="锂矿")["total"] == 1
    assert query_news(con, source="news")["total"] == 0

def test_content_truncated_and_flagged(con):
    insert_news(con, [_item(external_id="x", content="字" * 3000)])
    row = query_news(con)["items"][0]
    assert len(row["content"]) == 2000
    assert row["quality_flags"] & 2

def test_missing_time_falls_back(con):
    insert_news(con, [_item(external_id="y", published_at=None)])
    row = query_news(con)["items"][0]
    assert row["published_at"] is not None
    assert row["quality_flags"] & 1
```

- [ ] **Step 2 跑失败**:`python -m pytest tests/unit/test_news_store.py -v` → FAIL(No module named lquant.news)

- [ ] **Step 3 实现**

`src/lquant/news/model.py`:

```python
"""资讯条目模型。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class NewsItem:
    source: str          # telegraph / news / social / report
    source_name: str     # cls / sina_7x24 / em / ...
    external_id: str
    title: str
    content: str
    url: str
    symbols: tuple[str, ...] = ()
    industry_code: str | None = None
    published_at: datetime | None = None
    collected_at: datetime | None = None
    quality_flags: int = 0
    source_tag: str = ""

    @property
    def news_id(self) -> str:
        return hashlib.sha1(f"{self.source_name}|{self.external_id}".encode()).hexdigest()
```


`src/lquant/news/store.py`:按 Interfaces 与测试实现:`_DDL`(news_item 表,13 列)、`init_news_ddl`、`insert_news`(截断 2000 打 bit2,时间兜底打 bit1,INSERT OR IGNORE,新增数=插入前后 count 差)、`query_news`(参数化 where + list_contains + LIMIT/OFFSET + ORDER BY published_at DESC)、`news_stats_by_industry`、`news_stats_by_source`(GROUP BY source, source_name)。

- [ ] **Step 4 通过**:pytest → PASS

- [ ] **Step 5 Commit**

```bash
git add src/lquant/news/ tests/unit/test_news_store.py
git commit -m "feat: news 模块 DDL 与 news_item 存取层"
```

---

### Task 2: 行业/个股关联 link.py + news.yaml

**Files:**
- Create: `src/lquant/news/link.py`, `config/schema/news.yaml`
- Test: `tests/unit/test_news_link.py`

**Interfaces:**
- Consumes: Task 1 NewsItem
- Produces:
  - `link_symbols(item: NewsItem, name_to_code: dict[str,str]) -> NewsItem`(命中→symbols 去重合并 + flags|4;无命中原样返回)
  - `link_industry(item: NewsItem, kw_map: dict[str,str], symbol_to_industry: dict[str,str]) -> NewsItem`(关键词优先,其次 symbols 反查;都无→原样)
  - `build_name_to_code(securities_df) -> dict[str,str]`({name: symbol})
  - 正则按名称长度降序编译,模块级缓存 key=sorted names tuple

- [ ] **Step 1 写失败测试** `tests/unit/test_news_link.py`:

```python
from lquant.news.model import NewsItem
from lquant.news.link import link_symbols, link_industry, build_name_to_code

def _item(text):
    return NewsItem(source="news", source_name="em", external_id="1",
                    title=text, content="", url="u")

def test_link_symbols_multi_hit():
    n2c = {"平安银行": "000001.SZ", "宁德时代": "300750.SZ"}
    it = link_symbols(_item("宁德时代与平安银行合作"), n2c)
    assert set(it.symbols) == {"000001.SZ", "300750.SZ"}
    assert it.quality_flags & 4

def test_link_symbols_longest_first():
    n2c = {"银行": "999999.SZ", "平安银行": "000001.SZ"}
    it = link_symbols(_item("平安银行年报"), n2c)
    # "平安银行" 优先于前缀 "银行"
    assert it.symbols == ("000001.SZ",)

def test_link_no_hit_unchanged():
    it = link_symbols(_item("无命中文本"), {"平安银行": "000001.SZ"})
    assert it.symbols == ()
    assert it.quality_flags == 0

def test_link_industry_priority():
    kw = {"锂矿": "BK1"}
    s2i = {"300750.SZ": "BK2"}
    kw 命中 → BK1;无 kw 命中但 symbols 含 300750.SZ → BK2;都无 → None
    it1 = link_industry(_item("锂矿扩产 宁德时代受益"), kw, s2i)
    assert it1.industry_code == "BK1"
    it2 = link_industry(_item("宁德时代发新品"), kw, s2i)  # 无 symbols 时反查不生效
    it3 = link_industry(_item("x"), kw, s2i)
    assert it2.industry_code is None and it3.industry_code is None
    it4 = NewsItem(source="news", source_name="em", external_id="1",
                   title="x", content="", url="u", symbols=("300750.SZ",))
    it5 = link_industry(it4, kw, s2i)
    assert it5.industry_code == "BK2"
```

(注:test_link_industry_priority 里 it2 断言的注释即规格:反查只作用于 item.symbols。)

- [ ] **Step 2 跑失败** → FAIL(No module named lquant.news.link)

- [ ] **Step 3 实现 link.py**

```python
"""资讯与个股/行业关联:词表匹配 + 关键词映射 + 反查。"""
from __future__ import annotations

import re
from dataclasses import replace

from lquant.news.model import NewsItem

_BIT_INFERRED = 4

_MATCHER_CACHE: dict[tuple, re.Pattern] = {}

def _matcher(names) -> re.Pattern:
    ordered = sorted(set(names), key=len, reverse=True)
    return re.compile("|".join(re.escape(n) for n in ordered))

def link_symbols(item, name_to_code) -> NewsItem:
    if not name_to_code:
        return item
    key = tuple(sorted(name_to_code))
    if key not in _MATCHER_CACHE:
        _MATCHER_CACHE[key] = _matcher(name_to_code.keys())
    pat = _MATCHER_CACHE[key]
    text = f"{item.title or ''}{item.content or ''}"
    hits = list(dict.fromkeys(m.group(0) for m in pat.finditer(text)
                              if m.group(0) in name_to_code))
    if not hits:
        return item
    merged = tuple(dict.fromkeys([*item.symbols, *(name_to_code[n] for n in hits)]))
    return replace(item, symbols=merged, quality_flags=item.quality_flags | _BIT_INFERRED)

def link_industry(item, kw_map, symbol_to_industry) -> NewsItem:
    text = f"{item.title or ''}{item.content or ''}"
    for kw, code in sorted(kw_map.items(), key=lambda kv: -len(kv[0])):
        if kw in text:
            return replace(item, industry_code=code,
                           quality_flags=item.quality_flags | _BIT_INFERRED)
    for sym in item.symbols:
        if sym in symbol_to_industry:
            return replace(item, industry_code=symbol_to_industry[sym],
                           quality_flags=item.quality_flags | _BIT_INFERRED)
    return item

def build_name_to_code(securities_df) -> dict[str, str]:
    return {str(r["name"]).strip(): str(r["symbol"])
            for r in securities_df.iter_rows(named=True) if r["name"]}
```

- [ ] **Step 4 news.yaml 初始内容**

```yaml
# config/schema/news.yaml — 资讯模块配置(用户可编辑)
industry_keywords:
  锂矿: BK101010
  光伏: BK102010
  半导体: BK103010

industry_names:
  BK101010: 锂电
  BK102010: 光伏
  BK103010: 半导体

em_news:
  pool_limit: 200
```

(示例代码占位,BK 代码落地时换成真实申万/GILS 行业代码。)

- [ ] **Step 5 通过 → Commit**

```bash
git add src/lquant/news/link.py config/schema/news.yaml tests/unit/test_news_link.py
git commit -m "feat: 资讯与个股/行业关联(词表匹配+关键词映射+反查)"
```

---

### Task 3: NewsSource 协议 + telegraph 采集器

**Files:**
- Create: `src/lquant/news/sources/__init__.py`, `base.py`, `telegraph.py`
- Test: `tests/unit/test_news_source_telegraph.py`

**Interfaces:**
- `NewsSource`(Protocol):name: str, category: str, fetch(day: date) -> list[NewsItem]
- `register` 装饰器 + `get_sources(names=None) -> list`(未知 name → KeyError)
- `ClsTelegraphSource`:name="cls_telegraph", category="telegraph";akshare cls 失败 → fallback 新浪 7x24
- `SinaTelegraphSource`:name="sina_7x24", category="telegraph",独立可注册调用

- [ ] **Step 1 写失败测试**(akshare 列名实现前实测:
`python -c "import akshare as ak; print(ak.stock_telegraph_cls().columns.tolist())"`,
mock 列名按实测修正):

```python
import pandas as pd
from unittest.mock import patch
from datetime import date
from lquant.news.sources.telegraph import ClsTelegraphSource, SinaTelegraphSource

def test_cls_maps_columns():
    fake = pd.DataFrame({"日期": ["2026-09-10"], "时间": ["10:00:00"],
                         "内容": ["央行开展XX操作"], "标题": [None]})
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_telegraph_cls.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].source == "telegraph"
    assert items[0].source_name == "cls"
    assert "央行" in items[0].content
    assert items[0].external_id  # 日期+时间拼接

def test_sina_fallback_and_direct():
    fake = pd.DataFrame({"时间": ["2026-07-10 10:00:00"], "内容": ["新浪快讯"]})
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_telegraph_cls.side_effect = RuntimeError("blocked")
        mock_ak.stock_info_global_sina.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].source_name == "sina_7x24"
    assert items[0].published_at is not None
```

- [ ] **Step 2 跑失败 → 实现**

`base.py`:

```python
"""NewsSource 协议与来源注册表。"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from lquant.news.model import NewsItem

class NewsSource(Protocol):
    name: str
    category: str
    def fetch(self, day: date) -> list[NewsItem]: ...

_REGISTRY: dict[str, type] = {}

def register(cls):
    _REGISTRY[cls.name] = cls
    return cls

def get_sources(names=None) -> list:
    if names is None:
        names = list(_REGISTRY)
    missing = set(names) - set(_REGISTRY)
    if missing:
        raise KeyError(f"unknown news source: {missing}")
    return [_REGISTRY[n]() for n in names]
```


`telegraph.py`:ClsTelegraphSource.fetch 内 `try: ak.stock_telegraph_cls()` except → sina fallback;时间列 parse(格式实测确认),失败→published_at=None;external_id=日期+时间拼接或 sha1(content)[:16];SinaTelegraphSource 同映射逻辑,source_name="sina_7x24"。

- [ ] **Step 3 通过 → Commit**

```bash
git add src/lint-lquant/news/sources/ tests/unit/test_news_source_telegraph.py
git add src/lquant/news/sources/ tests/unit/test_news_source_telegraph.py
git commit -m "feat: telegraph 采集器(财联社 + 新浪7x24 fallback)"
```

---

### Task 4: em_news 采集器

**Files:**
- Create: `src/lquant/news/sources/em_news.py`
- Test: `tests/unit/test_news_source_em_news.py`

**Interfaces:**
- Consumes: base.py register
- Produces: `EmNewsSource(pool: list[str] | None = None)`:name="em_news", category="news";pool=None 时 fetch 内调 `get_active_pool()` 懒加载;每股 NewsItem 直接带 symbols=(symbol,),**不打** bit4(来源精确关联)
- `get_active_pool(limit=200) -> list[str]`:daily_bar 最近交易日成交额 top N;表缺失/空 → [](不 raise)

- [ ] **Step 1 写失败测试:**

```python
import pandas as pd
from unittest.mock import patch
from datetime import date
from lquant.news.sources.em_news import EmNewsSource, get_active_pool

def test_em_news_maps_and_tags_symbol():
    fake = pd.DataFrame({
        "关键词": ["平安银行"], "新闻标题": ["平安银行年报"],
        "新闻内容": ["..."], "新闻链接": ["http://x"],
        "发布时间": ["2026-09-10 10:00:00"],
    })
    with patch("lquant.news.sources.em_news.ak") as mock_ak, \
         patch("lquant.news.sources.em_news.get_active_pool", return_value=["000001.SZ"]):
        mock_ak.stock_news_em.return_value = fake
        items = EmNewsSource(pool=["000001.SZ"]).fetch(date(2026, 9, 10))
    assert items[0].symbols == ("000001.SZ",)
    assert not (items[0].quality_flags & 4)
    assert items[0].source_name == "em"

def test_get_active_pool_reads_daily_bar():
    内存库:建 daily_bar 建表插入两行(amount 100/200)→ limit=1 → 返回高成交额那只
    内存库 fixture:CREATE TABLE daily_bar(symbol VARCHAR, trade_date DATE, amount DOUBLE)
    插入两行,断言 get_active_pool(limit=1) == ["200 那只"]
    表缺失 → []
```

- [ ] **Step 2 跑失败 → 实现 → 通过**

- [ ] **Step 3 Commit**

```bash
git add src/lquant/news/sources/em_news.py tests/unit/test_news_source_em_news.py
git commit -m "feat: em_news 个股新闻采集器(成交额 top N 股票池)"
```

---

### Task 5: news_task 状态机

**Files:**
- Create: `src/lquant/news/tasks.py`
- Test: `tests/unit/test_news_tasks.py`

**Interfaces:**
- Consumes: Task 1 store、Task 2 link、Task 3/4 sources 注册表
- Produces:
  - `init_news_task_ddl(con) -> None`
  - `create_task(con, kind, params) -> dict`:kind ∈ {daily, manual};存在 pending/running → TaskConflictError(ValueError 子类)
  - `execute_task(con, task_id, runner 注入) -> dict`:runner(day, source_name) -> list[NewsItem];None → 默认 runner 从注册表 get_sources([src])[0].fetch
  - `retry_task(con, task_id, runner=None) -> dict`:只重跑 sources_status 里 failed 的 source;非 partial/failed/interrupted → TaskConflictError
  - `mark_interrupted_on_startup(con) -> int`(pending/running → interrupted)

- [ ] **Step 1 写失败测试**(runner 记录调用列表;参照 test_data_tasks.py 风格):

```python
import duckdb
import pytest
from lquant.news.model import NewsItem
from lquant.news.store import init_news_ddl
from lquant.news.tasks import (
    init_news_task_ddl, create_task, execute_task, retry_task,
    TaskConflictError, mark_interrupted_on_startup,
)

@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_news_ddl(c)
    init_news_task_ddl(c)
    return c

def _ni(ext, name="s1"):
    return NewsItem(source="telegraph", source_name=name, external_id=ext,
                    title=ext, content="", url="u")

def test_partial_then_retry_only_failed(con):
    calls = []
    def runner(day, src):
        calls.append(src)
        if src == "s1":
            return [_ni("a"), _ni("b")]
        raise RuntimeError("boom")

    t = create_task(con, "manual", {"sources": ["s1", "s2"]})
    out = execute_task(con, t["task_id"], runner=runner)
    assert out["status"] == "partial"
    assert out["rows_written"] == 2
    assert out["sources_status"]["s1"]["status"] == "ok"
    error 明细
    assert "boom" in out["sources_status"]["s2"]["error"]

    out2 = retry_task(con, t["task_id"], runner=runner)
    assert calls.count("s1") == 1          # s1 未被重跑
    assert calls.count("s2") == 2          # s2 被重试一次
    assert out2["status"] == "ok"
    assert out2["rows_written"] == 2

def test_dedupe_rerun_zero_rows(con):
    def runner(day, src):
        return [_ni("a"), _ni("b")] if src == "s1" else []

    t = create_task(con, "manual", {"sources": ["s1"]})
    execute_task(con, t["task_id"], runner=runner)
    t2 = create_task(con, "manual", {"sources": ["s1"]})
    out = execute_task(con, t2["task_id"], runner=runner)
    assert out["rows_written"] == 0        # news_id 去重命中

def test_all_failed_and_conflict(con):
    def runner(day, src):
        raise RuntimeError("down")

    t = create_task(con, "manual", {"sources": ["s1"]})
    out = execute_task(con, t["task_id"], runner=runner)
    assert out["status"] == "failed"
    with pytest.raises(TaskConflictError):
        create_task 互斥 → TaskConflictError:
        create_task(con, "manual", {"sources": ["s1"]})
    retry 对 failed 允许:
    out2 = retry_task(con, t["task_id"], runner=runner)
    assert out2["status"] == "failed"

def test_interrupted_on_startup(con):
    t = create状态:
    t = create_task(con, "running 残留 → interrupted
    t = create_task(con, "manual", {"sources": ["s1"]})
    con.execute("UPDATE news_task SET status='running' WHERE task_id=?", [t["task_id"]])
    assert mark_interrupted_on_startup(con) == 1
    row = con.execute("SELECT status FROM news_task WHERE task_id=?",
                      [t["task_id"]]).fetchone()
    assert row[0] == "interrupted"
```


- [ ] **Step 2 实现 tasks.py 要点**

```python
news_task DDL:
CREATE TABLE IF NOT EXISTS news_task (
    task_id VARCHAR PRIMARY KEY,
    kind VARCHAR,             -- daily / manual
    params JSON,
    status VARCHAR,           -- pending/running/ok/partial/failed/interrupted
    sources_status JSON,      -- {src: {status, rows, error}}
    rows_written INTEGER,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    message VARCHAR
)

execute_task 流程:
  校验 status=pending → UPDATE running
  day = params.get("date") or today
  加载关联输入(一次性):
    n2c = build_name_to_code(AkshareProvider().securities())
    kw_map = load_news_config()["industry_keywords"]
    s2i = industry_classify 每股取 max(std_date) 行:SELECT symbol, code FROM industry_classify
      WHERE (symbol, std_date) IN (SELECT symbol, max(std_date) FROM industry_classify GROUP BY symbol)
  for src in params["sources"]:
      try:
          items = runner(day, src)
          link 管线:已有 symbols 跳过 link_symbols;已有 industry_code 跳过 link_industry
          linked = []
          for it in items:
              x = it if it.symbols else link_symbols(it, n2c)
              x = x if x.industry_code else link_industry(x, kw_map, s2i)
              linked.append(x)
          rows = insert_news(con, linked)
          sources_status[src] = {"status": "ok", "rows": rows}
      except Exception as e:
          sources_status[src] = {"status": "failed", "error": str(e)}
  聚合状态(全 ok→ok / 全挂→failed / 混合→partial)+ UPDATE news_task
```

- [ ] **Step 3 通过 → Commit**

```bash
git add src/lquant/news/tasks.py tests/unit/test_news_tasks.py
git commit -m "feat: news_task 状态机(create/execute/retry/互斥/interrupted)"
```

---

### Task 6: API 路由 server/api/news.py

**Files:**
- Create: `src/lquant/server/api/news.py`
- Modify: `src/lquant/server/main.py`(include_router)
- Test: `tests/unit/test_api_news.py`(db 隔离参照 test_api_data_tasks.py 的 fixture)

**Endpoints:**
- `GET /api/news/items?source=&industry=&&symbol=&day=YYYY-MM-DD&keyword=&limit=&offset=` → query_news;limit>200 → 422,keyword>100 → 422
- `GET /api/news/industries` → news_stats_by_industry + industry_name(news.yaml industry_names)
- `GET /api/news/sources` → news_stats_by_source 与注册表并集(0 行来源也出现)
- `GET /api/news/tasks?limit=20` → 最近任务行
- `POST /api/news/tasks` body {kind:"manual", date?, sources?} → create + execute 同步执行(M1 无后台线程);TaskConflictError → 409
- `POST /api/news/tasks/{task_id}/retry` → retry_task;TaskConflictError → 409
- `GET /api/news/summary?day=` → 计数 by source/category/industry top10
- 路由内部首次访问懒 init 两个 DDL(仿 ensure_collect_log)

- [ ] **Step 1 写失败测试**(POST tasks 网络隔离:monkeypatch `lquant.news.tasks._default_runner` 为 fake)

```python
def test_items_empty_ok(client)
def test_items_validation: limit>200 → 422;keyword>100 → 422
def test_post_task_with_mocked_runner(client, monkeypatch):
    monkeypatch.setattr("lquant.news.tasks._default_runner", lambda day, src: [])
    POST /api/news/tasks {"kind": "manual"} → 200 ok 0 行
    再 POST → 409(互斥)
    retry 对 ok 任务 → 409
def test_summary(client): 插数据断言计数
```

- [ ] **Step 2 实现 news.py + main.py include_router + 通过**

- [ ] **Step 3 Commit**

```bash
git add src/lquant/server/api/news.py src/lquant/server/main.py tests/unit/test_api_news.py
git commit -m "feat: /api/news 路由(items/industries/sources/tasks/summary + 触发/重试)"
```

---

### Task 7: 前端 types + 组件 + lib

**Files:**
- Create: `web/src/app/news/{types,lib}.ts`, `SourceBadge.tsx`, `NewsFeedList.tsx`
- Test: `web/src/app/news/__tests__/{SourceBadge,NewsFeedList}.test.tsx`, `lib.test.ts`

**Interfaces:**
- types:NewsSourceKind = "telegraph"|"news"|"social"|"report";NewsItemDTO 与 Task 6 items 契约对齐
- SourceBadge:props { kind };四色 token(telegraph=绿/news=蓝/social=紫/report=橙)
- NewsFeedList:props { items, total, limit, offset, onLoadMore }
- lib.ts:fetchItems/fetchIndustries/fetchSources/fetchTasks/triggerTask/retryTask,统一 getData() 解包

- [ ] **Step 1 测试先行(仿 TaskBadge.test.tsx):**
SourceBadge renders 4 kinds;NewsFeedList renders items + 空态 + onLoadMore;lib mock fetch。

- [ ] **Step 2 实现 → `cd web && npx tsc --noEmit && npx vitest run` → PASS**

- [ ] **Step 3 Commit**

```bash
git add web/src/app/news/
git commit -m "feat(news-web): 来源徽章 + 时间线列表 + fetch 封装"
```

---

### Task 8: /news 页面三 tab + Sidebar 入口

**Files:**
- Create: `web/src/app/news/page.tsx`, `IndustryPanel.tsx`, `SymbolSearch.tsx`
- Modify: `web/src/components/Sidebar.tsx`(数组成员 { href: "/news", icon: Newspaper })
- Test: `web/src/app/news/__tests__/IndustryPanel.test.tsx`, `SymbolSearch.test.tsx`

**Interfaces:**
- page.tsx:三 tab(全部流/行业/个股),useState 切换,过滤状态跨 tab 保留
- IndustryPanel:左行业列表(计数+中文名),点击 → NewsFeedList(industry=code)
- SymbolSearch:先看 watchlist/page.tsx 有无现成搜索组件;无则 input + 现有搜索端点(实现时 grep 确认)
- StockNewsPanel 可并入 page.tsx(不必单独文件)

- [ ] **Step 1 测试:点击行业 → fetchItems({industry: code});选股 → fetchItems({symbol});Sidebar 含 /news**

- [ interlude: **Step 2 实现 → tsc + vitest 全量 → PASS**

- [ ] **Step 3 Commit**

```bash
git add web/src/app/news/ web/src/components/Sidebar.tsx
git commit -m "feat: /news 页面三 tab(全部流/行业/个股)+ Sidebar 资讯入口"
```

---

### Task 9: 集成 e2e

**Files:**
- Create: `tests/integration/test_news_e2e.py`(参照 test_backfill_task_e2e.py 结构)

- [ ] **Step 1 写 e2e:**

- akshare 全 mock(cls 假 DataFrame 含"平安银行";securities() mock 返回 平安银行→000001.SZ)
- POST /api/news/tasks → GET /api/news/items?symbol=000001.SZ 命中
- GET /api/news/summary 计数正确
- link 管线真实跑,断言 symbols/industry_code

- [ ] **Step 2 跑全量 pytest → Commit**

```bash
git add tests/integration/test_news_e2e.py
git commit -m "test: 行业资讯 e2e(采集→入库→关联→API)"
```

---

## Out of scope

- 社媒/研报采集器(M2)、AI 摘要/情绪(M3)、后台定时调度、异步线程执行
- 历史回补:akshare 电报/新闻接口只给最近数据,news_item 从启用日起累积
- 正文全文抓取
