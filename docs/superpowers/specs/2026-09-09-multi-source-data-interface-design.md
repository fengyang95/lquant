# 声明式多源数据接入机制设计

日期：2026-09-09
状态：已与用户确认

## 背景与目标

lquant 的数据层已有骨架：统一 schema（`src/lquant/data/schema.py`）、Capability
声明（`capability.py`）、DataProvider ABC（`base.py`）、providers.yaml 优先级与
限速、Fallback 链。但存在缺口：

- providers 目录下只有 `baostock.py`（479 行），akshare 在 yaml 中声明了却无实现
- tushare / tickdb 完全未接入
- 每个新源需要手写数百行列名转换代码，接入成本高
- schema 与源字段映射散落在 provider 代码里，没有统一维护点

**目标**：新源接入 ≈ 一份字段映射 YAML + 一个薄 fetch 类；schema 单点维护，
启动时校验映射合法性。

**范围**：baostock 迁移到新机制；akshare、tushare 完整接入（daily +
minute_bar 全频率 + adj_factor + financial_pit + reference + trade_calendar，
以各源实际能力为准）；tickdb 只保证机制就绪（yaml 预留条目）。

tushare 为积分制 API，需要 token（env `TUSHARE_TOKEN`），部分接口有积分门槛
与每分钟调用限制——token 缺失时该 provider 不加入链，而按 providers.yaml 的
`env_key` 机制声明；限速通过现有 qps/ratelimit 机制配置。

## 非目标

- 不替换 schema 载体（不 YAML 化、不引入 pydantic）——`schema.py` 的 dict 定义
  保持为唯一事实源
- 不改动 Fallback / HealthTracker / 质量管线（quality/）
- 本次不实现 tickdb 的 fetch 逻辑

## 架构

```
config/providers.yaml          # 源优先级/限速/开关（不变）
config/schema/<table>.yaml     # 新增：每张统一表的字段映射
src/lquant/data/
  schema.py                    # 表结构事实源，新增 validate_mapping
  base.py                      # DataProvider ABC（不变）
  mapping.py                   # 新增：FieldMapping 加载/校验/应用
  normalize.py                 # 映射归一化与现有清洗合并
  providers/
    _engine.py                 # 新增：MappingProvider 通用基类
    baostock.py                # 迁移：只留取数 + 声明 source 名
    akshare.py                 # 新写：同样模式，完整接入
    tushare.py                 # 新写：同样模式，完整接入（token 走 env）
```

## 字段映射 YAML

每张统一表一个文件，按源分节。本次实现 daily_bar 与 minute_bar 两份映射
作为模板，其余表（financial_pit / security 等）在对应 capability 实装时
按同样结构补充。

三类操作，按序应用：

1. **rename**：源列名 → 统一列名
2. **derive**：派生/换算列，`expr` 只允许白名单函数（算术、`concat`、
   `strptime` 等显式列出），`from` 声明依赖的源列
3. **fill**：常量填充（如 sec_type）

### 日级配置（config/schema/daily_bar.yaml）

```yaml
table: daily_bar
sources:
  baostock:
    rename:
      code: symbol
      date: trade_date
      open: open
      high: high
      low: low
      close: close
      preclose: pre_close
      volume: volume          # baostock 已是股
      amount: amount          # baostock 已是元，直接 rename（不换算）
    fill: {sec_type: stock}
    required: [symbol, trade_date, open, high, low, close, volume]
  akshare:
    rename:
      股票代码: symbol
      日期: trade_date
      开盘: open
      收盘: close
      最高: high
      最低: low
      成交量: volume          # akshare 日线手 → 股
    derive:
      volume: {expr: "volume * 100", from: [volume]}
      amount: {expr: "成交额", from: [成交额]}          # 已是元，仅显式声明
    fill: {sec_type: stock}
    required: [symbol, trade_date, open, high, low, close, volume]
```

### 分钟级配置（config/schema/minute_bar.yaml）

**一张 `minute_bar` 表承载全部频率**（1/5/15/30/60 分钟，60 分钟即小时级），
`freq` 列区分，映射 YAML 按源写一份，`freq` 由 fetch 层作为参数传入 fill，
而不是每条频率一张表。

时间语义统一约定：

- `ts` = **bar 结束时刻**（10:45 的 15 分钟 bar 覆盖 10:30~10:45）
- 60 分钟 bar 对齐到 11:00 / 14:00 / 15:00（含午休边界处理：baostock 的
  hour 线切在 11:30/14:00，需要合并 10:00-11:30 与 13:00-14:00 两段；
  akshare 东财 60 分钟线天然按 11:00/11:30/14:00/15:00 切，迁移时在
  `_post_normalize` 中归一到统一边界）

```yaml
table: minute_bar
sources:
  baostock:
    rename:
      code: symbol
      time: ts_raw
    derive:
      ts: {expr: "strptime(ts_raw, '%Y-%m-%d %H:%M:%S')", from: [ts_raw]}
      volume: {expr: "volume", from: [volume]}         # 已是股
      amount: {expr: "amount", from: [amount]}          # 已是元
    required: [symbol, ts, open, high, low, close, volume]
  akshare:
    rename:
      股票代码: symbol
      时间: ts_raw
      开盘: open
      收盘: close
      最高: high
      最低: low
      成交量: volume          # 手 → 股
      成交额: amount
    derive:
      ts: {expr: "strptime(ts_raw, '%Y-%m-%d %H:%M:%S')", from: [ts_raw]}
      volume: {expr: "volume * 100", from: [volume]}
    required: [symbol, ts, open, high, low, close, volume]
```

`freq` 参数化填充：engine 调 `_fetch_raw(table, freq=...)`，fetch 类将统一
freq 翻译为源参数并填入返回 df 的 `freq` 列：

| 统一 freq | baostock | akshare |
|---|---|---|
| 1min | 不支持（不声明 MINUTE_1 能力） | period="1" |
| 5min | frequency="5" | period="5" |
| 15min | frequency="15" | period="15" |
| 30min | frequency="30" | period="30" |
| 60min | frequency="60" | period="60" |

非 bar 类表（financial_pit / security / industry_classify / etf_meta）映射
文件结构相同，本次实现 daily_bar 与 minute_bar 两份作为模板，其余表在对应
capability 实装时按同样结构补充。

## mapping.py 职责

- 加载 `config/schema/<table>.yaml`，产出不可变的 `TableMapping`（frozen
  dataclass）
- `validate_mapping(table, mapping)`：启动时校验
  - 映射目标列必须存在于 `schema.py` 的该表 schema
  - `required` 列 ⊆ schema 列，且每源 required ⊆ rename/derive/fill 覆盖结果
  - derive 表达式仅使用白名单函数与 `from` 声明的列
  - 校验失败抛 `MappingError`（fail-fast，绝不静默降级）

## 通用适配器引擎

```python
class MappingProvider(DataProvider):
    """子类只做三件事：
    1. 声明 source 名（对应 yaml 的 sources key）
    2. 实现 _fetch_raw：调 SDK、翻页、处理原始参数
    3. 必要时覆写 _post_normalize hook（如财务长表拆分）
    归一化全部由 engine 完成：raw fetch → apply mapping → schema.coerce
    """
    source: str = ""
    def _fetch_raw(self, table: str, **params) -> pl.DataFrame: ...
    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        return df
```

`schema.py` 增强：新增 `validate_mapping`；`coerce` 保留，engine 强制在返回前
调用。

## 各源落地

| 源 | 动作 |
|---|---|
| baostock | 现有 479 行迁移：fetch 逻辑保留，列转换剥离到 yaml；映射文件覆盖其声明的全部 capability |
| akshare | 完整接入：daily_bar、minute_bar（1/5/15/30/60min）、adj_factor、financial_pit、security、trade_calendar、etf_daily；providers.yaml 中改为可配置启用 |
| tushare | 完整接入：daily_bar、minute_bar（1/5/15/30/60min，受积分门槛约束的频率在 fetch 层显式报错）、adj_factor（adj_factor 接口）、financial_pit（income/balancesheet/cashflow 长表拆分走 `_post_normalize`）、security（stock_basic）、trade_calendar（trade_cal）、etf_daily（fund_daily）；token 走 env `TUSHARE_TOKEN`，缺失则不加入链；qps 按积分档位配置 |
| tickdb | 机制就绪：yaml 预留条目 + 映射文件中预留节（注释），接入只需 yaml + fetch 类 |

## 错误处理

- 映射校验失败：启动即抛 `MappingError`，指明表、源、原因
- 源返回缺列：按 required 声明判错（required 缺列报错，非 required 补 None）
- 沿用现有 `CapabilityMissing` / watchdog / ratelimit 机制，不在 engine 重复

## 测试计划

- `tests/unit/test_mapping.py`：加载、校验失败路径（未知列、非法表达式、
  required 不合法）、rename/derive/fill 行为、不可变性
- `tests/unit/test_engine.py`：假 SDK 返回脏数据 → 归一化后符合 schema；
  `_post_normalize` hook 生效
- baostock 迁移：现有 golden 测试（`quality/golden.py`）回归对拍
- akshare / tushare：mock SDK 的单元测试（覆盖列名映射、单位换算、频率
  翻译、token 缺失时不入链）
- **对拍测试**：akshare / tushare / baostock 三源对同一标的同一交易日
  （mock 数据）产出后按 quality/crosscheck 的 tolerance 互比，保证映射
  换算正确

## 验收标准

1. 新增一个数据源 = 一个映射 YAML 节 + 一个实现 `_fetch_raw` 的类，无 schema
   修改
2. 映射配置错误在启动时暴露，而非运行时
3. baostock 迁移后现有测试全绿
4. akshare / tushare 可配置启用并产出符合统一 schema 的日线与分钟线数据
5. tushare token 缺失时启动不报错、该源不出现在 Fallback 链中
