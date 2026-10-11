# 缠论（日线）接入设计

- **日期**：2026-10-10
- **范围**：个股分析页「技术指标」域，新增日线级别缠论结构
- **状态**：待评审

## 1. 背景与目标

个股分析页（`web/src/app/security/[symbol]/page.tsx`）当前提供注册表驱动的技术指标（MA/MACD/RSI/KDJ/BOLL/量能…）与多角度分析评分。本次要加入**缠论（缠中说禅）**相关内容，**仅日线级别**，不做多级别递归。

缠论的产出是**分段结构**（分型 / 笔 / 中枢 / 背驰 / 买卖点），不是逐行标量。本仓指标注册表的契约是「每行一个标量列」，且 `src/lquant/indicators/registry.py:16` 的模块注释已明确写下：

> 指标里有相当一部分是**有状态/分段**结构（中枢、浪型、通道），用 `Ts_*` 算子表达不出来。

因此缠论**不进 `INDICATORS` 注册表**，而是独立引擎 + 独立 API + 独立前端面板。

### 目标

1. 日线级别输出：包含处理 → 分型 → 笔 → 中枢 → MACD 面积背驰 → 一二三类买卖点。
2. K 线图上可视化：笔折线、中枢矩形、分型标记、信号事件点。
3. 独立「缠论结构」面板：当前结构摘要 + 近期信号事件。
4. 不改变既有综合评分口径（缠论只展示，不参与评分）。

### 非目标

- **线段（XD/Seg）**：czsc 1.0.1 **未实现**（见 §3.2），本次不做。
- 多级别递归（30 分钟 / 5 分钟 / 周线）。
- 缠论参与 `technical_angle` 评分或选股筛选。
- 增量实时更新（每次请求全量重算即可，见 §5.4）。

## 2. 关键决策记录

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | 自研 vs 引入 czsc | **引入 czsc**，optional extra | 缠论算法细节多、流派多、易错；czsc 是 Apache-2.0、专为 A 股、活跃维护、且与 lquant 同为「Rust 核 + pyo3 + polars」架构。实测与 polars 1.44 共存无碍（§3.1） |
| D2 | 线段 | **放弃** | czsc 1.0.1 无实现（§3.2）；线段是缠论中争议最大、最主观的一环 |
| D3 | 展示形态 | 结构化 API + K 线叠加 + 独立面板 | 中枢/笔是区间结构，压成标量列会丢失语义 |
| D4 | 是否参与评分 | **不参与** | 缠论买卖点判据主观性强；且未完成笔会让分数随最新一根 K 线抖动，污染已稳定的综合分 |
| D5 | 未完成笔 | **只展示已确认结构** | 用 `c.bi_list[:-1]`（**无条件丢弃末笔**）—— 实测 czsc 自带的 `finished_bis` 启发式**不满足**前缀不变性（26 检查点 3 处违例），而 `bi_list[:-1]` 为 0 违例。详见 §3.5 |
| D6 | 信号集 | MACD 面积背驰 + 一/二/三类买卖点（6 类全要） | 覆盖缠论核心买卖点体系 |
| D7 | 前端渲染 | 自绘 canvas 叠加在现有 lightweight-charts K 线上 | 保持既有 K 线风格与时间轴对齐；本仓用 lightweight-charts v4.2.3，v4 无自定义 series 插件 API，overlay canvas 是唯一合理路径 |
| D8 | 依赖形态 | optional extra `chan` | 与 `sources`/`factors`/`ml` 惯例一致；未安装时优雅降级 |

## 3. 依赖与可行性验证（已实测）

### 3.1 czsc 1.0.1 与本仓共存

在隔离环境实测（`polars==1.44.1` + `czsc==1.0.1`）：

- **导入正常**，`CZSC` / `format_standard_kline` / `generate_czsc_signals` 均可用。
- **边界不跨界传 polars**：`format_standard_kline(df: pandas.DataFrame, freq) -> list[RawBar]`，输入是 pandas、输出是 Rust 对象。czsc 内部编译期锁 polars 0.52 只存在于它自己的 `.so` 内，与本仓的 polars 1.44 互不干扰。
- 400 根合成日线 → 182 分型 / 32 笔 / 7 中枢；6 类目标信号全部能触发。

**依赖增量**（净增，实测 `du`）：

| 包 | 大小 | 本仓是否已有 |
|---|---|---|
| czsc | 53M | 新增 |
| wbt | 48M | 新增 |
| plotly | 39M | 新增 |
| openpyxl / typer / dill / requests / requests-toolbelt / tqdm | ~10M | 新增 |
| scipy 100M / statsmodels 43M / pandas 49M / pyarrow 132M | — | 已有（pandas/pyarrow 核心依赖，scipy/statsmodels 在 `factors` extra） |

### 3.2 czsc 1.0.1 能力边界（源码核实）

| 能力 | 状态 | 证据 |
|---|---|---|
| 包含处理 `remove_include` | ✅ | `crates/czsc-core/src/analyze/utils.rs:177` |
| 分型 `FX` / `check_fxs` | ✅ | `utils.rs:264`、`utils.rs:303` |
| 笔 `BI` / `check_bi` | ✅ | `utils.rs:343` |
| 中枢 `ZS` / `get_zs_seq` | ✅ | `objects/zs.rs:21`、`utils.rs:31` |
| 未完成笔 `ubi` | ✅ | `analyze/mod.rs:567` |
| 虚拟笔 `FakeBI` | ✅ | `objects/fake_bi.rs` |
| **线段 XD/Seg** | ❌ **不存在** | `objects/mod.rs` 无 `xd`/`seg` 模块；全仓 grep `线段/特征序列/seg_list` 在代码中零命中 |
| 背驰 | ⚠️ 仅信号 | `crates/czsc-signals/src/zdy.rs`、`tas.rs` |
| 买卖点 | ⚠️ 仅信号 | `crates/czsc-signals/src/{zdy,cxt}.rs` |

### 3.3 口径差异（必须在代码与 UI 中显式声明）

- **MACD 柱口径**：czsc 的 `hist = DIF − DEA`（**未 ×2**，TA-Lib 口径，`czsc-signals/src/utils/ta.rs:291`，并有测试 `test_calc_macd_talib_histogram_is_not_doubled` 断言）。本仓 `add_macd(cn_hist=True)` 是国内的 `2×(DIF−DEA)`。
  - **影响评估**：背驰判据比的是**面积比值**（`bi2_area <= bi1_area * th/100`）与 **DIF 值**，同倍缩放不改变比值，因此**背驰结论不受影响**。但 UI 上若同时展示本仓 MACD 柱与 czsc 背驰信号，用户可能困惑 → 面板需加一行口径说明。
- **`min_bi_len` 语义**：默认 6，计的是**合并后 K 线**从 `fx_a` 首根到 `fx_b` 末根的根数（含两端分型各 3 根），不是「两个分型中点之间的根数」。
- **笔的价格约束**：`check_bi` 对 `fx_a`/`fx_b` 的唯一价格约束是「两者 `[low, high]` 区间不构成严格包含」，**没有缺口要求**。
- **中枢延伸**：`zg`/`zd` 由**前 3 笔**冻结，之后只要新笔仍与冻结区间重叠就延伸；**没有「9 笔」规则**。

### 3.4 前缀不变性实测（决定 D5 的关键证据）

用 700 根合成日线，在 18–26 个等距检查点上比对「只用前缀 `bars[:t]` 算出的结构」与「用全量算出的结构」：

| 取法 | 违例数 | 结论 |
|---|---|---|
| `c.finished_bis`（czsc 自带「已确认」） | **3 / 26** | ❌ 不可用 |
| `c.bi_list[:-1]`（无条件丢末笔） | **0 / 26** | ✅ 采用 |
| `get_zs_seq(bi_list[:-1])`，按 `sdt` 对齐比 `zg`/`zd` | **0 / 18** | ✅ 采用 |
| `generate_czsc_signals(...)` 逐日信号值 | **0 / 18** | ✅ 采用 |

两点必须记录在案：

1. **`finished_bis` 的启发式不可靠**。其定义是「`bars_ubi.len() < 5` 时丢掉末笔，否则全留」（`analyze/mod.rs:169-177`）。当 `bars_ubi.len() >= 5` 时它把一个**仍可能被推翻**的笔当成已确认输出，于是在后续数据到来时该笔消失 —— 正是未来函数的反面。`bi_list[:-1]` 无条件丢末笔，才真正满足「已确认的笔永不改变」。
2. **中枢的「延伸」不是违例**。`get_zs_seq` 用单笔起头再延伸，因此同一中枢在不同时点的 `edt`/`n_bis` 会增长 —— 这是缠论中枢的正常演化。**前缀不变性判据只约束 `sdt`/`zg`/`zd`**（实测三者完全稳定）。测试与实现都必须按这个口径写，否则会把正确行为误判成 bug。

### 3.5 配置默认值

> `init_n` 的实测结论见 §3.6。

| 常量 | 默认 | 本仓取值 |
|---|---|---|
| `min_bi_len` | 6 | 6（显式传入，不依赖 czsc 的环境变量） |
| `max_bi_num` | 50 | **200**（默认 50 会截断历史，见 §5.4） |
| MACD fast/slow/signal | 12/26/9 | 12/26/9 |
| 背驰面积阈值 `th` | 50 (%) | 50 |
| 二买 MA | SMA/21 | SMA/21 |
| 三买 MA | SMA/34 | SMA/34 |

> czsc 支持 `CZSC_MIN_BI_LEN` / `CZSC_MAX_BI_NUM` 环境变量覆盖。本仓**显式传参**，避免环境变量造成不可复现的行为差异。

### 3.6 `init_n` 的真实语义（信号覆盖）

`generate_czsc_signals(bars, ..., init_n=N)` 的 `init_n` 是**跳过的前 N 根**，不是「预热根数」。实测（700 根输入）：

| `init_n` | 返回行数 | 覆盖区间 |
|---|---|---|
| 0 | 700 | 全窗口 |
| 500 | 200 | 后 200 根 |
| ≥ 输入长度 | 1 | 仅最后一根 |

两条实测结论决定了实现取 `init_n=0`：

1. **`init_n` 不改变信号取值**：`init_n=0` 与 `init_n=500` 在 200 个共同交易日上**逐日完全一致**（0 差异）。
   > 踩坑记录：首次比对得出「57 天不一致」，实为探针 bug —— czsc 返回的 dict **键顺序不稳定**，按 dict 迭代序组 tuple 会把同一行比成错位的。必须按**显式列名**比对。
2. **`init_n=0` 同样满足前缀不变性**（0 违例，与 `init_n=500` 一致）。

因此取 `init_n=0`：喂多少根就有多少根信号，调用方不必为预热额外多取数据。

## 4. 架构

```
┌─ 前端 ─────────────────────────────────────────────┐
│ security/[symbol]/page.tsx                         │
│   ├─ ChanPanel.tsx        （新）结构摘要 + 信号事件 │
│   └─ KChart.tsx           （扩展）chan prop        │
│        └─ ChanOverlay canvas 图层（新）             │
└────────────────────┬───────────────────────────────┘
                     │ GET /security/{symbol}/chan
┌────────────────────┴───────────────────────────────┐
│ server/api/security.py  （追加路由）                │
└────────────────────┬───────────────────────────────┘
┌────────────────────┴───────────────────────────────┐
│ indicators/chan.py   （新，唯一适配层）             │
│   compute_chan() -> ChanResult                     │
│     ├─ _to_pandas()   polars → pandas（列名映射）   │
│     ├─ _build_czsc()  format_standard_kline + CZSC │
│     ├─ _extract_*()   fx / bi / zs                 │
│     └─ _signals()     generate_czsc_signals + 沿变 │
└────────────────────┬───────────────────────────────┘
                     │ 函数级惰性 import
                 [czsc]  optional extra
```

**边界原则**：`czsc` 只在 `src/lquant/indicators/chan.py` 内被 import，且为**函数级惰性 import**（本仓惯例，见 `pyproject.toml` ruff `PLC0415` ignore 的注释「函数级 import 是有意的惰性加载模式（CLI/服务启动提速）」）。这样未安装 czsc 时，平台其余部分零影响。

## 5. 详细设计

### 5.1 引擎适配层 —— `src/lquant/indicators/chan.py`

```python
@dataclass(frozen=True)
class ChanFx:      # 分型
    dt: date; mark: Literal["G", "D"]; value: float

@dataclass(frozen=True)
class ChanBi:      # 笔
    sdt: date; edt: date; direction: Literal["up", "down"]
    high: float; low: float; n_bars: int

@dataclass(frozen=True)
class ChanZs:      # 中枢
    sdt: date; edt: date
    zg: float; zd: float; gg: float; dd: float; n_bis: int

@dataclass(frozen=True)
class ChanSignalEvent:   # 信号事件（沿变点，非状态）
    dt: date; kind: str; label: str; detail: str

@dataclass(frozen=True)
class ChanResult:
    symbol: str; asof: date | None; n_bars: int
    fx: tuple[ChanFx, ...]; bi: tuple[ChanBi, ...]; zs: tuple[ChanZs, ...]
    latest: dict[str, str | None]
    events: tuple[ChanSignalEvent, ...]
    notes: tuple[str, ...]
    def to_dict(self) -> dict: ...   # JSON-ready，date → "YYYY-MM-DD"
```

公开入口：

```python
def compute_chan(bars: pl.DataFrame, *, symbol: str = "",
                 min_bi_len: int = 6, max_bi_num: int = 200,
                 signal_kinds: Sequence[str] = DEFAULT_SIGNAL_KINDS) -> ChanResult
```

`signal_kinds` 是**本仓自定义的 kind 名**（见下表左列），模块内 `_SIGNAL_CFG: dict[str, list[dict]]` 把它映射成 czsc 的 `signals_config`（右列的 `{name, freq}` 列表）；未注册的 kind 抛 `ValueError`。API 层不暴露该参数，恒用 `DEFAULT_SIGNAL_KINDS`。

实现要点：

1. **`_to_pandas(bars)`** —— 列名映射 `trade_date→dt`、`volume→vol`；`amount` 缺失时用 `close * volume` 兜底（czsc 强制要求 8 列，缺一即 ValueError）；`dt` 用 `pl.Series.dt.date()` 落地为 `date`，避开时区（czsc 返回 tz-aware，实测为 `+00:00`）。
2. **`_build_czsc(pdf)`** —— `format_standard_kline(pdf, Freq.D)` → `CZSC(bars, min_bi_len=min_bi_len, max_bi_num=max_bi_num)`。
3. **`_extract_fx/bi(c)`** —— 取 **`c.bi_list[:-1]`**（D5 + §3.4：无条件丢弃末笔，才满足前缀不变性；**不要用 `finished_bis`**）。分型取每笔的 `fx_a`/`fx_b`，按 `dt` 去重排序。笔的 `direction` 由 `BI.direction`（`Up`/`Down`）映射为 `"up"`/`"down"`。
4. **`_extract_zs(c)`** —— **自建** `czsc.get_zs_seq(c.bi_list[:-1])`，**不要用 `c.zs_list`**（它基于 `finished_bis`，继承了 §3.4 的违例）。再**过滤 `len(z.bis) >= 3`**：`get_zs_seq` 用单笔起头再延伸，末位中枢常只有 1–2 笔，此时 `zg`/`zd` 由不足 3 笔算出，是退化值（实测 600 根数据末位中枢 `n_bis=2` 却仍给出 zg/zd）。过滤后 `zg >= zd` 恒成立。
5. **`_signals(bars)`** —— `generate_czsc_signals(bars, signals_config=cfg, sdt=<bars 首根日期 YYYYMMDD>, df=False)`；解析每行信号串的 `v1` 段（`"其他_任意_任意_0"` → `v1="其他"`）。`sdt` 取输入首根 K 线日期，保证返回行覆盖全窗口。
   - **沿变检测**：信号是**状态**（实测三卖可连续 57 天同值），只在「上一行 ≠ 具体值 且 本行 = 具体值」时记一条 `ChanSignalEvent`，避免图上被状态点淹没。
   - `latest` 取最后一行各信号的 `v1`。
6. **`notes`** —— 固定附上 §3.3 的两条口径说明（线段缺失、MACD 柱口径）。

**信号配置（`DEFAULT_SIGNAL_KINDS`）**：

| kind | czsc 信号名 | `v1` 取值 |
|---|---|---|
| `beichi` | `zdy_macd_bc_V230422` | 上涨 / 下跌 |
| `buy1` / `sell1` | `cxt_first_buy_V221126` / `cxt_first_sell_V221126` | 一买 / 一卖 |
| `buy2` / `sell2` | `cxt_second_bs_V230320` | 二买 / 二卖 |
| `buy3` / `sell3` | `cxt_third_bs_V230318` + `cxt_third_buy_V230228` | 三买 / 三卖 |

### 5.2 API —— `GET /security/{symbol}/chan`

挂在既有 `src/lquant/server/api/security.py`（`APIRouter(prefix="/security")`）下，与 `/security/{symbol}/analysis` 并列。

**请求**：`symbol: str`（复用 `deps.resolve_symbol`），`limit: int = 250`（`le=500`，与 `/data/indicators` 对齐）。

**取数**：`read_daily([sym])` 取**全量**历史再截尾 —— 与 `/data/indicators` 同策略（指标有预热期，只喂 `limit` 根会让头部结构缺失）。**不需要为信号额外多取**：`generate_czsc_signals` 的 `init_n` 取 0 时全窗口都出信号，且实测 `init_n` 不改变信号取值（§3.6）。

**响应**（`available=true`）：`ChanResult.to_dict()`。

**降级**（`available=false`）：

```jsonc
{ "symbol": "...", "available": false, "reason": "未安装 czsc，请 `uv sync --extra chan`" }
{ "symbol": "...", "available": false, "reason": "日线不足 30 根，无法构成笔" }
```

**错误**：`symbol` 未解析 → 沿用 `resolve_symbol` 既有行为（404）。

### 5.3 前端

**`ChanPanel.tsx`（新）**：

- 结构摘要：最近 N 笔（方向 / 起止 / 幅度）、当前中枢（`[zd, zg]` 区间 + 已延伸笔数）、`latest` 六类信号状态徽标。
- 事件列表：近期 `events`（日期 / 类型 / 标签 / 明细），倒序。
- 口径说明：渲染 `notes`（线段未实现、MACD 柱口径）。
- 降级态：`available=false` 时显示 `reason`，不阻塞页面其余部分（对齐 `IndicatorPickerFallback` 的做法）。

**`KChart.tsx`（扩展）**：

- 新增可选 prop `chan?: ChanOverlayData`（`{ fx, bi, zs, events }`），**缺省不传时行为与现在完全一致**（向后兼容，既有调用点零改动）。
- 新增内部组件 `ChanOverlay`：在图表容器内绝对定位一层 `<canvas>`，与 lightweight-charts 的 canvas 同尺寸，`pointer-events: none`。
- 坐标换算：`chart.timeScale().timeToCoordinate(date)` → x；`candleSeries.priceToCoordinate(price)` → y。
- 重绘触发：`useEffect` 依赖 `[chan, bars]`；另订阅 `timeScale().subscribeVisibleLogicalRangeChange` 与 `ResizeObserver`，在平移/缩放/尺寸变化时重绘（否则叠加层会与主图错位）。
- 绘制内容：
  - **笔折线**：相邻笔端点连折线（`direction` 决定颜色，阳红阴绿，与 K 线一致）。
  - **中枢矩形**：`x ∈ [sdt, edt]`，`y ∈ [zd, zg]`，半透明填充 + 描边；`gg`/`dd` 用更淡的虚线标出延伸极值。
  - **分型标记**：顶分型在 `high` 上方画 ▽、底分型在 `low` 下方画 △。
  - **信号事件点**：在对应 K 线的价格位置画小圆点 + 文本标签（一买/三卖/顶背驰…），买卖点用不同颜色区分。
- **图表清理**：`chart.remove()` 时同步移除 canvas（现有 effect 的 cleanup 里追加）。

### 5.4 性能（已实测，含一处必要的设计修正）

7000 根（30 年）日线的分阶段实测：

| 阶段 | 耗时 |
|---|---|
| `CZSC` 结构构建 | 105 ms |
| `_extract_bi` / `_extract_zs` | 9 / 12 ms |
| **`generate_czsc_signals`（全窗口）** | **1709 ms** ← 瓶颈 |
| 合计（全窗口信号） | **~1.8 s** ← 不可接受 |

**修正**：`generate_czsc_signals` 支持 `init_n`（跳过的前 N 根）。用它把**信号计算限制在展示窗口内**：

| 信号范围 | 耗时 |
|---|---|
| 全窗口（7000 根） | 1709 ms |
| 只算尾部 250 根 | **78 ms**（22×） |

**该优化是安全的**：实测 `init_n` **不改变信号取值**（300 个共同交易日 0 差异），只决定从哪根开始算（§3.6）。因此 `compute_chan(..., signal_tail=limit)` 既拿到正确取值，又把耗时压到 ~200 ms。

其余要点：

- **结构仍用全量历史**（CZSC 只要 105 ms），因为中枢的正确性依赖完整前文。
- **`max_bi_num=200`**：czsc 默认 50 会从头部裁剪笔、并连带丢弃更早的原始 K 线（`analyze/mod.rs:229-239`）。取 200 保证中枢序列不被截断。
- **不引入缓存**（YAGNI）：~200 ms 已在可接受区间；若日后成为瓶颈，再按 `symbol + 末根日期` 加进程内 LRU。

**真实数据湖实测**（`600519.SH`，425 根；`000001.SZ`，1152 根）：

| 阶段 | 耗时 |
|---|---|
| `import czsc`（**进程内仅一次**） | 2349 ms |
| `read_daily` | 233 ms |
| `compute_chan` | ~140 ms |
| `security_chan` **稳态** | **~160 ms** |

结论：稳态 160 ms 达标。**冷启动的 2.3 s 是 czsc 导入，每个服务进程只付一次** —— 不加处理的话，首屏表现为「缠论计算中…」多转 2 s。

**处理：启动时预热**。`server/main.py` 的 `_startup` 拉起一个 **daemon** 线程跑 `chan.warmup()`（只导入 czsc，不做缓存），把导入成本挪到服务启动之后：

- 不违背「重依赖惰性 import 提速启动」的约定 —— 启动本身**不等**这个线程，CLI 路径完全不受影响。
- 用 daemon 是刻意的：本仓在 non-daemon 线程上踩过坑（见 `tests/conftest.py` 对 aiosqlite worker 的注释），daemon 不会拖住解释器退出。
- 未装 czsc（optional extra）时只打印一行降级提示；预热自身出错也只打印，绝不阻断启动。
- 线程体抽成模块级 `_warm_chan_engine()` 以便**直调测试** —— 写在线程闭包里就只能靠异步等待断言，既慢又易 flaky。

## 6. 测试策略

**后端**（`tests/unit/test_indicators_chan.py`，用确定性合成数据，不依赖网络）：

1. **抽取与序列化契约**：`bi` 方向严格交替、`zs` 满足 `zg >= zd` 且 `n_bis >= 3`、时间序单调、`to_dict()` 输出可 JSON 序列化。
2. **前缀不变性（关键）**：`compute_chan(bars[:t])` 的**已确认结构**不随 `bars[t:]` 变化。`future.py` 的 `check_prefix_invariance` 是**列级**的（`df -> df`），缠论返回结构体，故在测试内实现**结构级**判据，口径按 §3.4：
   - 笔：`(sdt, edt, direction, high, low)` 五元组必须完全一致。
   - 中枢：按 `sdt` 对齐，只比 `zg`/`zd`（`edt`/`n_bis` 的延伸不算违例）。
   - 信号：逐日 `v1` 值一致。
   这是缠论接入最容易被绕过、也最必须守住的一条，直接对应 D5。
3. **沿变检测**：构造一段信号连续多日同值的数据，断言只产生 1 个事件。
4. **优雅降级**：monkeypatch 掉 czsc 的 import，断言返回 `available=false` 且 `reason` 非空。
5. **列名映射与 `amount` 兜底**：缺 `amount` 列时用 `close*volume`。
6. **API 层**：`/security/{symbol}/chan` 正常返回、数据不足降级、`limit` 边界。

覆盖率门禁 95%（本仓既有门禁），适配层需全覆盖。

**前端**：

- `ChanPanel` 渲染测试（有数据 / 降级态）。
- 坐标换算抽成纯函数并单测（`web/src/lib/__tests__/chan.test.ts`）。
- `KChart` 向后兼容：不传 `chan` 时渲染结果不变（既有快照/断言不动）。
- 页面接线测试：按 symbol 拉 `/chan?limit=250`，且同一份数据同时给到面板与 K 线。

**e2e（`web/e2e/chan-canvas.spec.ts`，路由打桩，不需要后端）**：

单测验不了「canvas 到底有没有画出东西」—— 而 lightweight-charts v4 没有自定义 series
API，叠加层是手绘的，**画不出来时页面不报错、只是图上什么都没有**，属最难发现的一类缺陷。
所以补两个 Playwright 用例，直接读叠加层的 `getImageData` 数像素：

1. 叠加层真的画出了非透明像素（>50）。
2. 缩放后叠加层跟着重绘（用**像素签名哈希**比对，比像素计数可靠 —— 计数可能碰巧不变）。

> 踩坑记录：用例 2 起初误报「不重绘」。根因是 K 线在首屏之下，`boundingBox()` 给的是
> **视口坐标**，不先 `scrollIntoViewIfNeeded()` 的话鼠标事件落在图表之外，
> 连 K 线自己的 canvas 都不重绘。诊断方法：同时哈希主图 canvas 与叠加层 ——
> 两者都没变 = 交互没生效，而非叠加层有 bug。

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| czsc 版本漂移（1.0.x 刚发布，API 仍在动） | 升级可能破坏适配层 | `uv.lock` 锁死；`chan.py` 是唯一适配层，改动面收敛在一个文件 |
| czsc 自带的「已确认」口径（`finished_bis`）不满足前缀不变性 | 会引入未来函数 | **已规避**：改用 `bi_list[:-1]` + 自建 `get_zs_seq`（§3.4 实测 0 违例），并加结构级前缀不变性测试守住 |
| 中枢退化（`n_bis < 3`） | zg/zd 是退化值 | §5.1 第 4 点显式过滤 |
| 依赖体积（+145M） | 部署变重 | optional extra；未装时优雅降级 |
| canvas 叠加层与主图错位 | 视觉 bug | 订阅可见区间变化 + ResizeObserver 重绘；换算函数单测 |
| MACD 口径与本仓不一致 | 用户困惑 | 面板显式渲染 `notes` |

## 8. 交付清单

- `pyproject.toml`：新增 `[project.optional-dependencies].chan`
- `uv.lock`：同步（`uv lock`，仅新增 czsc 及其依赖）
- `.github/workflows/ci.yml`：`uv pip install -e ".[sources,factors,ml,server,dev,chan]"`
- `src/lquant/indicators/chan.py`：新（引擎适配层）
- `src/lquant/server/api/security.py`：追加 `/security/{symbol}/chan`
- `src/lquant/server/main.py`：启动时 daemon 线程预热缠论引擎
- `tests/unit/server_auth_open_surface.json`：新增端点的鉴权档位（`read:factors`）
- `web/src/components/ChanPanel.tsx`：新
- `web/src/components/KChart.tsx`：扩展 `chan` prop + canvas 叠加层
- `web/src/lib/chan.ts`：类型契约 + 几何纯函数（新）
- `web/src/app/security/[symbol]/page.tsx`：接入面板与叠加
- `tests/unit/test_indicators_chan.py`、`tests/unit/test_api.py`、`tests/unit/test_main_startup_fill.py`：后端测试
- `web/src/lib/__tests__/chan.test.ts`、`web/src/components/__tests__/ChanPanel.test.tsx`、`web/src/app/security/__tests__/page.test.tsx`：前端测试
- `web/e2e/chan-canvas.spec.ts`：叠加层像素级 e2e
