# qlib: Documentation, Ecosystem, and Limits

**Scope.** What `microsoft/qlib` is and how it is designed; the officially documented capability map; how data
(especially China A-share data) actually gets into qlib; ecosystem and community state; known limitations; which
parts are reusable without adopting the framework; and version/feature notes for `pyqlib` 0.9.7 vs upstream `main`.

**Method & sources.** Primary sources only where possible: the repo (`README.md`, `docs/*.rst`, `qlib/contrib/data/*`,
`scripts/data_collector/*`, `pyproject.toml`), [qlib.readthedocs.io](https://qlib.readthedocs.io/en/latest/), the GitHub
API (repo metadata, releases, commits, issue search), the [Qlib paper (arXiv:2009.11189)](https://arxiv.org/abs/2009.11189),
[PyPI](https://pypi.org/pypi/pyqlib/json), and the two community data/downstream repos named by qlib itself. Where I
could only reach a secondary or unverified source, that is stated explicitly.

**Environment note.** The local install is `pyqlib 0.9.7`. I could **not** import it in this session: the workspace
`.venv` and the user-level `python3.11`/`python3.12` interpreters all raise `ModuleNotFoundError: No module named 'qlib'`
(verified by running `python -c "import qlib"` against each). All version facts below therefore come from PyPI, the
GitHub API, and the source tree, not from the local interpreter. I did read this workspace's own qlib integration code
(`src/lquant/qlib_io/`, `src/lquant/factors/qlib_alpha.py`, `config/qlib/*.yaml`), which is used as first-hand evidence
for §6.

**Date caveat.** The GitHub API snapshot reports `pushed_at` 2026-09-22 and repo `updated_at` 2026-10-03, and the newest
commit I retrieved is dated 2026-09-16. I treat "now" as roughly **October 2026** and label it explicitly, because it
materially changes the "is it maintained?" answer.

---

## 1. What qlib is and its design philosophy

### 1.1 Official self-description

> "Qlib is an open-source, AI-oriented quantitative investment platform that aims to realize the potential, empower
> research, and create value using AI technologies in quantitative investment, from exploring ideas to implementing
> productions. Qlib supports diverse machine learning modeling paradigms, including supervised learning, market
> dynamics modeling, and reinforcement learning."
> — [README.md](https://github.com/microsoft/qlib/blob/main/README.md)

> "It contains the full ML pipeline of data processing, model training, back-testing; and covers the entire chain of
> quantitative investment: **alpha seeking, risk modeling, portfolio optimization, and order execution**."
> — [README.md](https://github.com/microsoft/qlib/blob/main/README.md)

The last sentence is the closest thing in the official material to a "data → model → strategy → execution" value chain,
and it is the phrasing I would rely on. See §1.3 for an important correction about the "alpha-seeking / risk-seeking /
execution layers" framing.

### 1.2 The paper's stated motivation and design pillars

The paper diagnoses three problems — (1) the AI-driven **research-workflow revolution** breaks tools designed for the
traditional split workflow; (2) **high-performance infrastructure** is required because AI is data-driven (TB-scale data
in high-frequency scenarios, thousands of derived features such as Alpha101 from only 5 raw dimensions); (3) **obstacles
to applying ML** in finance (extremely low signal-to-noise ratio, non-differentiable objectives such as annualized
return, and hyperparameter tuning cost). It then proposes three design pillars:

1. **AI-oriented framework** — modularised to match a modern research workflow, with default implementations per module,
   and *"designed to serve users as a platform rather than a toolbox"* (code, computation and data can be shared).
2. **High-performance infrastructure** — a time-series **flat-file database** dedicated to scientific computing, plus an
   **expression engine** so factors can be written as expressions rather than code.
3. **Guidance for machine learning** — curated datasets/tasks (feature space + target label) plus a Hyperparameters
   Tuning Engine (HTE) that reuses the previous best hyperparameters as a prior when models are periodically retrained.

Source: [arXiv:2009.11189 §2.1, §3.1](https://arxiv.org/abs/2009.11189).

### 1.3 The layer model — and a correction to the brief

The task brief refers to "the alpha-seeking / risk-seeking / execution layers from the paper." **I could not verify that
three-layer naming, and I believe it is not how qlib names its layers.** What I actually verified:

- **The paper's Figure 1 module list** (paper §3.2): Data Server → Data Enhancement → Model Creator → Model Manager →
  Model Ensemble → Portfolio Generator → Order Executor → Analyser, plus a **Dynamic Modeling** group for regularly
  updating models/strategies. The Order Executor is deliberately a *"responsive simulator rather than a back-testing
  function"* so RL can use it as an environment.
- **The documentation's layer names** ([introduction.rst](https://qlib.readthedocs.io/en/latest/introduction/introduction.html)):
  **Infrastructure layer** (DataServer, Trainer) → **Learning Framework layer** (Forecast Model, Trading Agent) →
  **Workflow layer** (Information Extractor, Forecast Model, Decision Generator, Execution Env) → **Interface layer**
  (Analyser). I independently confirmed these labels by grepping the source of
  [`docs/_static/img/framework.svg`](https://raw.githubusercontent.com/microsoft/qlib/main/docs/_static/img/framework.svg):
  the diagram contains the text labels `Infrastracture` [sic], `Learning Framework`, `Strategy`, `Workflow`,
  `Interface`, `Data Server`, `Information Extractor`, `Decision Generators`, `Forecast Model`, `Executor`,
  `Simulator`, `Environment`, `Analyser`, `Multi-level Workflow`, `Sub-workflow`.
- **"Alpha" and "Risk" appear as two sub-boxes of `Forecast Model`** in that same diagram — i.e. alpha and risk are two
  *signal types produced by the forecasting module*, not two platform layers.
- The literal strings `Seeking`, `seeking`, `alpha-seeking`, `risk-seeking` occur **0 times** in `framework.svg`, and do
  not appear in the paper text I fetched (paper §1–§4.1).

**Conclusion (high confidence):** describe qlib as *Infrastructure → Learning Framework → Workflow → Interface*, with the
value chain *alpha seeking / risk modeling / portfolio optimization / order execution* as the README's summary. The
"alpha-seeking layer / risk-seeking layer / execution layer" phrasing is best treated as a paraphrase, not qlib
terminology. If a decision document needs the three-layer story, it should attribute it to the README's chain, not to
the paper's layer names.

### 1.4 The learning paradigm as documented

Workflow-layer flow, per the docs: `Information Extractor` → `Forecast Model` (produces alpha and risk signals) →
`Decision Generator` (produces portfolio/orders) → `Execution Env` (the market/simulator), with the explicit option of
**nesting** levels ("an order executor trading strategy and intraday order executor could behave like an interday
trading loop and be nested in a daily portfolio management trading strategy and interday trading executor trading
loop"). Learning paradigms are split into supervised learning and RL; RL reuses the Workflow layer's `Execution Env` to
build environments and *"NestedExecutor is supported as well"*.
Sources: [introduction.rst](https://qlib.readthedocs.io/en/latest/introduction/introduction.html),
[README.md](https://github.com/microsoft/qlib/blob/main/README.md).

---

## 2. Official capability map (docs + README)

### 2.1 Data server and expression engine

- **Storage design**: a tree of flat files — `calendars/day.txt`, `instruments/<market>.txt`,
  `features/<symbol>/<field>.<freq>.bin`. Values are compact fixed-width binary so byte-indexing works; the **first 4
  bytes of each feature file are the start index into the shared calendar**, which is how series are aligned on time.
  Update = append; adding/removing instruments or attributes = adding/removing files.
  Sources: [paper §3.3.2](https://arxiv.org/abs/2009.11189),
  [data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst).
- **Expression engine**: factors are written as expressions over `$`-fields, e.g.
  `(Mean($close, N)+2*Std($close, N)-$close)/Mean($close, N)` for the Bollinger upper band. Operators are registered and
  user-extensible (`tests/test_register_ops.py`); the documented operator set includes `Ref, Mean, Sum, Std, Var, Skew,
  Kurt, Max, Min, IdxMax, IdxMin, Quantile, Med, Mad, Rank, Count, Delta, Slope, Rsquare, Resi, WMA, EMA, Corr, Cov,
  TResample`, plus elementwise/comparison ops.
  Sources: [paper §3.3.3](https://arxiv.org/abs/2009.11189),
  [API reference](https://qlib.readthedocs.io/en/latest/reference/api.html),
  [data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst).
- **Two-level cache**: an in-memory LRU cache over the parsed expression tree, plus a **disk expression cache** and a
  **disk dataset cache** (both indexable on the time dimension, so they stay valid when the query window shifts).
- **The performance claim**: same task (14 factors from 800 stocks × 2007–2020 daily OHLCV) — HDF5 184.4 s, MySQL 365.3 s,
  MongoDB 253.6 s, InfluxDB 368.2 s, vs **Qlib +Expression +Dataset cache 7.4 s (1 CPU)** and 4.2 s (64 CPU). This is
  qlib's own benchmark, reproduced in the README table.
  Source: [README.md](https://github.com/microsoft/qlib/blob/main/README.md).

### 2.2 Point-in-time (PIT) database

Documented as a first-class feature ("Point-in-Time database … Released on Mar 10, 2022", PR #343). Purpose: financial
statement values get restated, so using only the latest version for historical backtests leaks future information;
PIT storage keeps the version that was actually available at each historical timestamp, so *"online trading and
historical backtesting"* agree. Implementation is **file-based**: per feature, four columns `date, period, value, _next`
where `date` is the publication date, `period` is the reporting period (annual = year integer; quarterly =
`<year><quarter index>`), and `_next` is the byte offset of the next occurrence of the field; an `.index` file speeds up
queries; rows are sorted by publication date. A crawler + converter ships under `scripts/data_collector/pit/`.
Source: [advanced/PIT.html](https://qlib.readthedocs.io/en/latest/advanced/PIT.html).

*Relevance to A-share:* this is the mechanism for the 财报 restatement / 业绩快报-vs-正式报告 problem. Note that qlib
supplies the *mechanism and a PIT collector*, not A-share PIT fundamentals — you must supply the data.

### 2.3 Alpha158 / Alpha360

Both are `DataHandlerLP` subclasses in `qlib/contrib/data/handler.py`, whose features come from
`qlib/contrib/data/loader.py` (`Alpha158DL`, `Alpha360DL`). Verified from source:

- **Alpha360** = the last 60 days of `close, open, high, low, vwap, volume`, each normalised by the latest close (volume
  by latest volume): 6 fields × 60 days = 360 columns, named `CLOSE59…CLOSE0`, etc.
- **Alpha158** = **9 kbar features** (`KMID, KLEN, KMID2, KUP, KUP2, KLOW, KLOW2, KSFT, KSFT2`) + **price features**
  (default config `windows:[0]`, `feature:[OPEN, HIGH, LOW, VWAP]` → 4 columns) + **29 rolling families × 5 windows
  {5,10,20,30,60} = 145 columns** → 158 total. Families: `ROC, MA, STD, BETA, RSQR, RESI, MAX, MIN, QTLU, QTLD, RANK,
  RSV, IMAX, IMIN, IMXD, CORR, CORD, CNTP, CNTN, CNTD, SUMP, SUMN, SUMD, VMA, VSTD, WVMA, VSUMP, VSUMN, VSUMD`. The config
  is parameterised (`kbar`/`price`/`volume`/`rolling` with `windows`/`include`/`exclude`), so "Alpha158" is a default
  configuration of a general feature generator, not a fixed table.
- Both handlers are documented as available for **US and China** markets.
- **The label convention encodes A-share T+1**: the default label is `Ref($close,-2)/Ref($close,-1) - 1`, and the docs
  explain why — *"when getting the T day close price of a china stock, the stock can be bought on T+1 day and sold on
  T+2 day."* This is a documented, A-share-specific design decision.

Sources: [handler.py](https://raw.githubusercontent.com/microsoft/qlib/main/qlib/contrib/data/handler.py),
[loader.py](https://raw.githubusercontent.com/microsoft/qlib/main/qlib/contrib/data/loader.py),
[data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst).

### 2.4 Model zoo (as listed in the README)

Supervised / deep models: **XGBoost, LightGBM, CatBoost, MLP, LSTM, GRU, ALSTM, GATs, SFM, TFT, TabNet,
DoubleEnsemble, TCTS, Transformer, Localformer, TRA, TCN, ADARNN, ADD, IGMTF, HIST, KRNN, Sandwich.**
RL order execution: **TWAP, PPO, OPDS** (the last two from the cited papers). Dynamic/market-dynamics: **Rolling
Retraining (baseline)** and **DDG-DA**.
Source: [README.md](https://github.com/microsoft/qlib/blob/main/README.md), [examples/benchmarks](https://github.com/microsoft/qlib/tree/main/examples/benchmarks).

Caveat carried in the README itself: *"Each baseline has different environment dependencies … (e.g. TFT only supports
Python 3.6~3.7 due to the limitation of `tensorflow==1.15.0`)"*. So the zoo is **not uniformly runnable on one
environment** — a real integration cost (§5).

### 2.5 Workflow: `qrun` + MLflow experiment management

- `qrun` runs the whole workflow from one YAML file (build dataset → train → backtest → evaluate). The config sections
  are `qlib_init`, `market`, `benchmark`, `data_handler_config`, `dataset`, `model`, `record`. The workspace's own
  [`config/qlib/workflow_alpha158_lgbm.yaml`](../../../../../../config/qlib/workflow_alpha158_lgbm.yaml) is a live
  example of this format.
- **Recorder / experiment management**: `QlibRecorder` (imported as `R` from `qlib.workflow`) wraps a three-level model
  — `ExperimentManager` → `Experiment` → `Recorder` (one recorder per run) — with `log_params`, `log_metrics`,
  `log_artifact`, `save_objects`/`load_object`, `search_records`, `list_experiments`. The docs state plainly that the
  **concrete implementation is `MLflowExpManager`, "which is based on the machine learning platform MLflow"**, and that
  with it *"users can use the command `mlflow ui` to visualize and check the experiment results."*
- **Record templates** produce standard artefacts: `SignalRecord` (predictions), `SigAnaRecord` (IC, ICIR, Rank IC,
  Rank ICIR, long-short return), `PortAnaRecord` (backtest), plus `ACRecordTemp`, `HFSignalRecord`,
  `MultiPassPortAnaRecord`.
- The recorder docs have a short **Known Limitations** section: *"The Python objects are saved based on pickle, which
  may results in issues when the environment dumping objects and loading objects are different."*
Sources: [README.md](https://github.com/microsoft/qlib/blob/main/README.md),
[recorder.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/recorder.rst).

### 2.6 Online serving and rolling retraining

Announced in the README as "Online serving and automatic model rolling … Released on May 17, 2021" (PR #290). The
documented API surface (from the docs' API listing) is `OnlineManager`, `OnlineStrategy` / `RollingStrategy`,
`OnlineTool` / `OnlineToolR`, and updaters `RMDLoader, RecordUpdater, DSBasedUpdater, PredUpdater, LabelUpdater`. Two
deployment modes exist: **offline** (local data, the default) and **online** (a shared data service where data and
caches are shared across clients, consuming less disk and improving cache hit rate). Online mode is served by the
separate [qlib-server](https://github.com/microsoft/qlib-server) repo, deployable via Azure CLI scripts.

*Confidence note:* I retrieved the docs' page/API inventory for online serving but the full
`docs/component/online.rst` body failed to fetch twice (network error), so I describe only the documented component
names and the offline/online distinction, not the internals of the rolling schedule.

### 2.7 Nested decision execution / high-frequency backtest

Documented in [component/highfreq.html](https://qlib.readthedocs.io/en/latest/component/highfreq.html). The argument:
daily portfolio management and intraday order execution are usually studied separately, but joint backtest requires
them to interact; *"None of the publicly available high-frequency trading frameworks considers multi-level joint
trading."* qlib's design: each level = `Trading Agent` (Information Extractor + Forecast Model + Decision Generator) +
`Execution Env`, where the execution env can itself contain a finer-grained sub-workflow. Frequency, decision content
and execution environment are user-customisable. The doc also notes the *optimisation* coupling: a portfolio with higher
turnover may become better once execution improves — which is the reason to co-optimise levels, and which `QlibRL`
supports. Examples: `examples/nested_decision_execution/workflow.py`, `examples/highfreq`,
`examples/orderbook_data` (feature extraction from non-fixed-frequency high-frequency data).

### 2.8 RL for order execution

`qlib.rl` provides `Interpreter` / `StateInterpreter` / `ActionInterpreter`, `Reward` / `RewardCombination`,
`Simulator`, `Trainer` / `TrainingVessel` / `Checkpoint` / `EarlyStopping`, and an `order_execution` module
(`FullHistoryStateInterpreter`, `CurrentStepStateInterpreter`, `CategoricalActionInterpreter`,
`TwapRelativeActionInterpreter`, `PPO`, `PAPenaltyReward`, `SingleAssetOrderExecutionSimple`, `SAOEStrategy`,
`ProxySAOEStrategy`, `SAOEIntStrategy`). Baselines compared in `examples/rl_order_execution`: TWAP, PPO, OPDS.
Sources: [README.md](https://github.com/microsoft/qlib/blob/main/README.md),
[API reference](https://qlib.readthedocs.io/en/latest/reference/api.html).

### 2.9 Meta-learning (DDG-DA) for concept drift

Listed as "Meta-Learning-based framework & DDG-DA … Released on Jan 10, 2022" (PR #743), with
`examples/benchmarks_dynamic/DDG-DA`. The docs' **Meta Controller** component exposes `MetaTask` (+`get_meta_input`),
`MetaTaskDataset` (+`prepare_tasks`), `MetaModel`, `MetaTaskModel`, and `MetaGuideModel`. The README frames the problem
as non-stationarity: *"the data distribution may change in different periods, which makes the performance of models
build on training data decays in the future test data."* The paired baseline is plain **rolling retraining**.
Sources: [README.md](https://github.com/microsoft/qlib/blob/main/README.md),
[component/meta.html](https://qlib.readthedocs.io/en/latest/component/meta.html).

### 2.10 Portfolio / risk / execution model, and backtest realism knobs

- Strategies: `TopkDropoutStrategy` (documented algorithm: hold `Topk`, each day sell the `d` worst held names ranked
  outside `K` and buy the same number of best unheld names; typical turnover ≈ `2×Drop/Topk`), `EnhancedIndexingStrategy`
  (outperform a benchmark while controlling tracking error), plus `WeightStrategyBase` (subclass focuses only on target
  weights), `TWAPStrategy`, `SBBStrategyBase`, `SBBStrategyEMA`, `SoftTopkStrategy`.
- Evaluation: `risk_analysis`, `indicator_analysis`, `backtest_daily`, `long_short_backtest`.
- **Backtest realism knobs that qlib does expose** (from the docs' example configs): `limit_threshold` (**0.095** in
  `strategy.rst`; the data-mode table in `data.rst` says China = **0.099**, US = `None` — note this internal
  inconsistency), `deal_price` (`close`), `open_cost` 0.0005, `close_cost` 0.0015, `min_cost` 5, `account`, `benchmark`.
  The China mode table adds **`trade_unit = 100`** (A-share lot size) vs 1 for US.
- **Documented A-share-aware behaviours**: limit up/down threshold, 100-share trade unit, T+1-aware label convention
  (§2.3), and the convention that `open, close, high, low, volume, money and factor` are **set to NaN when the stock is
  suspended**. (Release v0.9.1 also notes a fix to *"allow sell in limit-up case and allow buy in limit-down case in
  topk strategy"*, PR #1407 — i.e. limit handling has had bugs and iteration.)
Sources: [strategy.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/strategy.rst),
[data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst),
[v0.9.1 release](https://api.github.com/repos/microsoft/qlib/releases).

**What the docs do *not* claim:** I found no documentation of T+1 sell-restriction enforcement inside the exchange,
of order cancellation on suspension, of capacity/market-impact modelling, or of borrow/short constraints. Absence of
documentation is not proof of absence in code — I did not read `qlib/backtest/exchange.py` — but a decision-maker
should treat these as **must-verify-in-code**, not as provided.

### 2.11 Report & analysis

`analysis_position.report`, `analysis_position.score_ic`, `analysis_position.risk_analysis`,
`analysis_model.analysis_model_performance`; graphical outputs include cumulative return by group, long-short return
distribution, IC / monthly IC / IC decay, forecast-signal autocorrelation, and portfolio backtest reports. The
`analysis` extra is needed (`pip install .[analysis]`).
Sources: [component/report.html](https://qlib.readthedocs.io/en/latest/component/report.html),
[README.md](https://github.com/microsoft/qlib/blob/main/README.md).

---

## 3. Data collection & sources, and the China A-share story

### 3.1 Collectors actually shipped on `main`

`scripts/data_collector/` contains: `yahoo/`, `fund/`, `cn_index/`, `us_index/`, `contrib/`, `pit/`, **`crowd_source/`**,
**`crypto/`**, **`br_index/`**, **`baostock_5min/`**, plus `base.py`, `index.py`, `utils.py`,
`future_calendar_collector.py`. The directory's own README lists only *yahoo, fund, cn_index, us_index, contrib* —
i.e. **the collector README is out of date relative to the directory** (a documentation-gap data point).
Sources: [contents API](https://api.github.com/repos/microsoft/qlib/contents/scripts/data_collector),
[data_collector/README.md](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/README.md).

Recent `main` commits show active work on **China data plumbing specifically**:

- `refactor(data_collector): use akshare to build unified trade calendar (#2093)` — 2026-01-20
- `fix: use baostock to fetch trading calendar instead of Eastmoney API (#2193)` — 2026-04-17
- `fix: incorrect index implementation in FileCalendarStorage (#2195)` — 2026-04-21
- `fix: value error caused by incorrect date format in daily data (#2015)` — 2026-04-15
- `feat: check lowercase naming for qlib features directories (#2087)` — 2026-01-19

and the `test` extra in `pyproject.toml` now includes `baostock`, `yahooquery`, `lxml<6.1.3`.
Sources: [commits API](https://api.github.com/repos/microsoft/qlib/commits?per_page=40),
[pyproject.toml](https://raw.githubusercontent.com/microsoft/qlib/main/pyproject.toml).

**Interpretation:** as of `main`, qlib's own China data story is (a) Yahoo for OHLCV, (b) **AkShare/Baostock for the
trading calendar**, (c) **Baostock for 5-minute bars**. That is a meaningful shift from the older "Yahoo only"
position and is the single most decision-relevant data finding for an A-share platform.

### 3.2 The official dataset is disabled — and the community replacement

The README states: *"❗ Due to more restrict data security policy. The official dataset is disabled temporarily. You can
try [this data source](https://github.com/chenditc/investment_data/releases) contributed by the community."* The
documented download is `qlib_bin.tar.gz` from that repo's latest release. The README also warns that the dataset *"is
collected from Yahoo Finance, and the data might not be perfect. We recommend users to prepare their own data if they
have a high-quality dataset"*, and that users **cannot incrementally update** the shipped offline data (fields are
stripped to reduce size) — you must re-download from the collector and then update incrementally.
Source: [README.md](https://github.com/microsoft/qlib/blob/main/README.md).

**`chenditc/investment_data` — the de-facto A-share data path.** Verified from its README:

- **What it is**: a crowd-sourced, cross-validated A-share dataset, published as qlib-format `qlib_bin.tar.gz` releases,
  with raw data hosted on DoltHub (`chenditc/investment_data`) and the processing scripts on GitHub.
- **Why**: qlib's own `scripts/data_collector/crowd_source/README.md` states the rationale — *"Public data source like
  yahoo is flawed, it might miss data for stock which is delisted and it might have data which is wrong. This can
  introduce survivorship bias into our training process."* Merging sources is meant to give a more complete history and
  to cross-validate anomalies.
- **Sources merged**: `w` (Wind, high quality but only until 2019), `c` (Caihui, also only until 2019), `ts` (Tushare),
  `ak` (AkShare), `yahoo`, `baostock`; tables prefixed by source, with a merged, validated `final` set.
- **A-share-specific content**: the validated tables include **`final_a_stock_eod_price`** and
  **`final_a_stock_limit`** (price-limit data) — the latter is directly useful for A-share limit-up/down realism.
- **Operational fragility (documented)**: daily updates require a **Tushare token**; the README solicits **VPS
  sponsorship** (*"30G+ memory, 4 core+ CPU and good connection to dolthub and github"*) to keep the CI/CD pipeline
  running; and it now documents manifest validation, digest-pinned publication via a GitHub workflow, a fail-closed
  rollback procedure, and a specific *"repair-2026-07-20"* operation. Read together, these indicate **supply-chain
  hardening after some incident** — I could not determine what happened, so I flag it as a risk to investigate rather
  than a confirmed event.
- A Chinese README and a Chinese blog post (WeChat) are linked, indicating a Chinese-speaking user base.
Sources: [investment_data README](https://raw.githubusercontent.com/chenditc/investment_data/main/README.md),
[crowd_source README](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/crowd_source/README.md).

### 3.3 How people actually get A-share data in: CSV/Parquet → qlib `.bin`

The documented universal path is `scripts/dump_bin.py dump_all`. Verified requirements from `data.rst`:

- Input is CSV **or Parquet**, either one file per symbol (`SH600000.csv` / `.parquet`, case-insensitive) **or** one file
  with a symbol column (then pass `--symbol_field_name`).
- A date column is mandatory (`--date_field_name`).
- Prices must be **adjusted**; the minimum field set is `open, close, high, low, volume, factor`, with
  `factor = adjusted_price / original_price`. Original price is recovered with `$close / $factor`.
- Extra fields are allowed and encouraged: *"If you want to use your own alpha-factor which can't be calculate by OHLCV,
  like PE, EPS and so on, you could add it to the CSV or Parquet files with OHLCV together and then dump it to the Qlib
  format data."* → **this is the documented hook for A-share valuation/flow fields (PE/PB/turnover/market cap).**
- Convention: `open, close, high, low, volume, money, factor` are NaN when the stock is suspended.
- Example invocation:
  `python scripts/dump_bin.py dump_all --data_path ~/.qlib/my_data --qlib_dir ~/.qlib/qlib_data/ --include_fields open,close,high,low,volume,factor --file_suffix .csv`
- Stock pools: `python collector.py --index_name CSI300 --qlib_dir <dir> --method parse_instruments`.
- Data health check: `python scripts/check_data_health.py check_data --qlib_dir <dir> [--freq 1min]` with thresholds
  `--missing_data_num`, `--large_step_threshold_price`, `--large_step_threshold_volume` (checks missing values, large
  step changes, missing required columns, missing `factor`).
- **Price-adjustment semantics matter**: qlib normalises each stock's price to **1 on its first trading day**, so
  adjusted prices differ across data sources; the docs link issue
  [#991 discussion](https://github.com/microsoft/qlib/issues/991#issuecomment-1075252402) on this. The workspace's own
  exporter instead normalises to **latest = raw** (前复权到最新) — a deliberate divergence worth knowing when comparing
  numbers against qlib benchmarks.
Sources: [data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst),
[export.py](../../../../../../src/lquant/qlib_io/export.py).

I did **not** verify first-hand tutorials using Tushare/AkShare/Baostock → qlib beyond the above (a targeted search
returned a CSDN comparative piece on Qlib/AkShare/Tushare/Baostock, but I did not fetch it). The *upstream-verified*
A-share ingestion routes are: `chenditc/investment_data` releases, qlib's own Yahoo collector, the `baostock_5min`
collector, and the generic `dump_bin.py` CSV/Parquet path.

### 3.4 The 1-minute / high-frequency story

- A **1-minute CN dataset** is documented: `python -m qlib.cli.data qlib_data --target_dir ~/.qlib/qlib_data/cn_data_1min --region cn --interval 1min`
  (or `scripts/get_data.py`). The README's feature timeline records "High-frequency data(1min) … Released on Jan 27,
  2021" and "High-frequency data processing example … Feb 5, 2021".
- **Baostock 5-minute** is the newer, self-service route: `scripts/data_collector/baostock_5min/README.md` documents a
  three-step pipeline — `collector.py download_data` (per-symbol CSV with
  `date, symbol, open, high, low, close, volume, amount, adjustflag`), `collector.py normalize_data` (normalise OHLC by
  adjclose **and** rescale so the first valid trading date's close = 1; for `interval=5min` you **must** pass a 1-day
  `qlib_data_1d_dir`), then `dump_bin.py dump_all --freq 5min`. A ready-made dataset is offered with `--region hs300
  --interval 5min`, and its `v2` version's end date is **2022-12** — i.e. the packaged HF data is stale, and current
  data requires running the collector yourself.
- Order-book / non-fixed-frequency data has an example (`examples/orderbook_data`) and an Arctic provider backend
  (README: "Arctic Provider Backend & Orderbook data example", Jan 17, 2022).
Sources: [README.md](https://github.com/microsoft/qlib/blob/main/README.md),
[baostock_5min README](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/baostock_5min/README.md).

**Practical caveat (my assessment, flagged as such):** qlib's HF story is *research-grade*. The 1-minute dataset is
Yahoo-derived, and the 5-minute packaged data ends 2022-12; production A-share minute bars would come from your own
vendor. qlib's contribution here is the **nested-execution backtest framework and the storage format**, not the data.

### 3.5 Data format / cache structure (reference)

```
data/
  calendars/day.txt
  instruments/all.txt, csi500.txt, ...
  features/<symbol>/<field>.day.bin
  calculated features/<symbol>/<hash(instrument, field_expression, freq)>/...  (+ .meta)
  cache/<hash(stockpool_config, field_expression_list, freq)>/...             (+ .meta, .index)
```
Source: [data.rst](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst).

---

## 4. Ecosystem & community

### 4.1 Metrics and cadence (GitHub API snapshot)

| Metric | Value |
|---|---|
| Stars | **49,108** |
| Forks | **7,758** |
| Watchers/subscribers | 511 |
| Open issues | **484** |
| License | MIT |
| Created | 2020-08-14 |
| `pushed_at` | 2026-09-22 |
| PyPI `Development Status` | **3 - Alpha** |

Source: [repos API](https://api.github.com/repos/microsoft/qlib), [PyPI JSON](https://pypi.org/pypi/pyqlib/json).

**Release cadence** (from the releases API; all are real tagged releases):

| Tag | Published |
|---|---|
| v0.9.7 | **2025-08-15** |
| v0.9.6 | 2024-12-23 |
| v0.9.5 | 2024-05-24 |
| v0.9.4 | 2024-05-07 |
| v0.9.3 | 2023-07-18 |
| v0.9.2 | 2023-06-25 |
| v0.9.1 | 2023-01-29 |
| v0.9.0 | 2022-12-09 |
| v0.8.6 | 2022-06-15 |

Reading: **~2 releases/year in 2022–2023, then ~1/year in 2024–2025, and no release in ~14 months as of Oct 2026.**
Activity has not stopped, but it has clearly slowed from the 2021–2022 peak.

**Commit activity on `main`** (most recent 20 retrieved, newest first):

`2026-09-16` ci: pin GitHub Actions to full-length SHAs (#2318) · ci: fix dependency compatibility failures (#2308) ·
`2026-07-23` feat(config): explicit validation for required config fields (#2078) ·
`2026-04-22` docs: replace broken RD-Agent demo links (#2150) ·
`2026-04-21` fix: incorrect index implementation in FileCalendarStorage (#2195) ·
`2026-04-17` fix: use baostock for trading calendar instead of Eastmoney (#2193) ·
`2026-04-15` fix: date-format ValueError in daily data (#2015) ·
`2026-03-10` fix(security): RestrictedUnpickler in load_instance (#2153) ·
`2026-02-12` fix(backtest): avoid calendar overflow when end_time missing (#2127) ·
`2026-02-04` fix: US symbols URL failure (#1975) ·
`2026-02-03` refactor: deterministic budget allocation in SoftTopkStrategy (#2077) ·
`2026-01-28` fix(security): unsafe pickle.load usages (#2099) ·
`2026-01-22` fix: ignore generated file when installing from source (#2091) ·
`2026-01-21` fix: semantic version comparison for PyTorch scheduler (#2094) ·
`2026-01-20` refactor(data_collector): akshare unified trade calendar (#2093) ·
`2026-01-19` feat: check lowercase naming for qlib features directories (#2087) ·
`2025-12-30` fix(security): restrict pickle deserialization to safe classes (#2076) ·
`2025-12-27` fix: handler_mod with None end date (#2068) ·
`2025-12-18` fix(client): missing dependencies and unsafe pickle usage (#2072) ·
`2025-11-18` fix(data_collector): us_index collector 403 Forbidden (#2047) · fix(filter): SeriesDFilter (#2051)

Source: [commits API](https://api.github.com/repos/microsoft/qlib/commits?per_page=40).

**Reading:** maintenance is **real but conservative** — the 2025-11 → 2026-09 window is dominated by dependency pins,
CI hardening, **security (pickle) fixes**, and small China-data/calendar fixes. There are **no new models and no major
new capabilities** in this window. Several months have zero commits (2026-05, 2026-06, 2026-08). This is a
"maintained, not actively evolving" profile.

### 4.2 Downstream / adjacent projects — verified vs. assumed

**Verified qlib-ecosystem projects:**

- **`microsoft/RD-Agent`** — the flagship. qlib's own README says qlib *"is now equipped with https://github.com/microsoft/RD-Agent to automate R&D process"* and announces RD-Agent as an LLM-based agent for **automated factor mining and model optimization**, with a paper (R&D-Agent-Quant, [arXiv:2505.15155](https://arxiv.org/abs/2505.15155)) and demo videos. A commit as recent as 2026-04-22 fixes RD-Agent demo links in qlib's README — so the relationship is live. **This is qlib's forward-looking capability story.**
- **`microsoft/qlib-server`** — the online-mode data server (separate repo), with Azure one-click deployment.
- **`chenditc/investment_data`** — the community A-share dataset (§3.2), explicitly blessed by qlib's README and its `crowd_source` collector.
- **`DulyHao/AlphaForge`** — appears in qlib issue #1927 as a project consuming qlib's `QlibDataLoader`/`D.operations` API directly (the reporter's `stock_data.py` imports qlib). Verified as a real qlib consumer, though I did not otherwise review it.

**Verified NON-relationships (corrections to plausible assumptions):**

- **TradingAgents (`TauricResearch/TradingAgents`) is *not* qlib-based and does not integrate qlib.** Its README (fetched in full) contains **no mention of qlib**; it is a LangGraph multi-agent LLM framework with its own analyst/researcher/trader/risk roles, its own Yahoo-Finance-based data vendors, its own memory log and backtest grid. It does support A-share tickers via Yahoo suffixes (`.SS`/`.SZ`). I could not verify whether qlib appears in the TradingAgents *paper* text (arXiv 2412.20138) — treat any claim that it "compares against qlib" as **unverified**.
- **FinGPT (`AI4Finance-Foundation/FinGPT`) is *not* qlib-related.** Its README contains **no mention of qlib**; it is a financial-LLM project (sentiment analysis, FinGPT-Forecaster), whose data/benchmark layers are HuggingFace datasets and FinNLP. Note it explicitly says FinGPT-Forecaster is trained on DOW-30 and that **China-market support would require separate data collection and fine-tuning** (with FinGPT v1.x "designed for Chinese markets").

Sources: [qlib README](https://github.com/microsoft/qlib/blob/main/README.md),
[TradingAgents README](https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md),
[FinGPT README](https://raw.githubusercontent.com/AI4Finance-Foundation/FinGPT/master/README.md).

**Chinese-language ecosystem.** Signals I verified: qlib issue titles and bodies are frequently in Chinese; the
`chenditc/investment_data` README has a Chinese translation and links a Chinese blog post; the README links Chinese
WeChat articles about qlib; and a Chinese Zhihu article about RD-Agent's strategy-R&D pipeline surfaced in search.
There are also auto-generated third-party docs (e.g. DeepWiki pages for `microsoft/qlib`). I did **not** verify a
specific, well-known A-share-focused qlib fork; the concrete A-share adaptation I verified first-hand is **this
workspace's own** `src/lquant/qlib_io/` + `src/lquant/factors/qlib_alpha.py` (§6).

### 4.3 Integration friction (verified, attributed)

- **Data acquisition is the top friction.** The official dataset is disabled; the recommended source is a
  single-maintainer community repo that needs a Tushare token and solicits VPS sponsorship. qlib's own `crowd_source`
  README concedes Yahoo-derived data causes survivorship bias.
- **You must convert your data into qlib's binary format.** `dump_bin.py` is the only supported path, with a strict
  column contract (adjusted prices + `factor`). This is a real one-time engineering cost, and the adjustment convention
  (first-day-normalised) may not match your vendor's.
- **Online mode is a separate deployment.** qlib-server is a different repo and Azure-oriented; the docs' online client
  example has a long-standing open bug (#184, `BadNamespaceError`, open since 2021-01-07, 12 comments) that includes a
  socket.io version-pinning complaint — a signal that online mode is less trodden than offline.
- **Dependency pinning churn.** `pyproject.toml` on `main` carries defensive pins with explanatory comments
  (`mlflow<3.13`, `filelock>=3.16.0,<3.30`, `fastjsonschema<2.22` for py<3.10, `osqp==1.0.5` for win32+py3.8,
  `plotly<7`, `scipy<=1.15.3`, `snowballstemmer<3.0`, `python-socketio<6`) — each comment describes an upstream release
  that broke qlib. Recent commits `#2308` (dependency compatibility, grpcio source builds on older macOS ARM64) and
  `#2318` (pin Actions to SHAs) continue that pattern.
- **Parallel processing broke for a real user.** Open bug **#1927** (created 2025-05-19, 12 comments, updated 2026-03-27,
  still open) reports `AttributeError: '_backend_args'` in `qlib/utils/paral.py::ParallelExt` with joblib 1.5.0 on
  Windows, triggered by loading a CSI500 dataset via `QlibDataLoader` — i.e. the *parallel data path*, not an exotic
  corner. The reporter supplied a working one-line fix; it remains unmerged.
- **Mac/Apple Silicon.** The README documents that M1 users must `brew install libomp` before building LightGBM, and
  commit #2308 explicitly adds workarounds for grpcio source builds on older macOS ARM64.
- **Windows.** `osqp==1.0.5` is pinned specifically for `win32 and python_version == '3.8'`, and #1927 is a Windows
  report — consistent with Windows being the least-tested platform.

---

## 5. Known limitations / criticisms

*Structure: each item is tagged **[documented]** (official source), **[primary-issue]** (a real GitHub issue/commit I
retrieved), or **[assessment]** (my inference). I deliberately avoid unsourced blog claims; where community opinion is
likely but I could not verify it, I say so.*

### 5.1 Data dependency & licensing
- **[documented]** The official dataset is **disabled** for data-security reasons; users are pointed at a community
  GitHub release. qlib no longer ships a first-party A-share dataset.
- **[documented]** The shipped/demo data is Yahoo-derived and *"might not be perfect"*; qlib recommends preparing your
  own data. The offline dataset **cannot be incrementally updated** (fields stripped) — you must re-crawl from scratch.
- **[documented]** Yahoo-derived data **misses delisted stocks → survivorship bias**, per qlib's own `crowd_source`
  README.
- **[documented]** The community replacement requires a **Tushare token** for daily updates and requests **VPS
  sponsorship**; it has recently added manifest/digest validation and a fail-closed rollback path (indicating
  supply-chain hardening). This is a **single-maintainer external dependency for the most critical input**.
- **[assessment]** Licensing is a genuine gap: qlib is MIT, but the data is not. Wind/Caihui-derived history in the
  crowd-sourced set (only through 2019) has unclear redistribution status for commercial use. This deserves legal review
  before relying on `investment_data` in production.

### 5.2 Alpha decay / performance realism
- **[documented]** The README's headline `qrun` LightGBM + Alpha158 result is **annualized excess return 0.1783 without
  cost / 0.1289 with cost**, IR 2.00 / 1.44, max drawdown −8.2% / −9.1%. `strategy.rst` shows a similar example at
  0.1524 / 0.1033.
- **[assessment]** These are *research* backtests on a survivorship-biased Yahoo universe with a simplified cost model
  (fixed `open_cost`/`close_cost`/`min_cost`, no market impact, no capacity limit). The gap between the with-cost and
  without-cost numbers (~5 pp/year) is itself the warning: costs are first-order, and the model omits the
  higher-order ones. Treat published benchmark numbers as an **upper bound**, not an expectation.
- **[uncertain]** Community critiques that qlib demo results overstate achievable live performance are plausible and I
  saw Chinese blog titles in search results consistent with this (e.g. a CSDN piece on "Qlib+backtrader 回测到实盘：那些被忽略的
  致命细节"), but **I did not fetch and verify those articles**, so I do not assert their content.

### 5.3 Maintenance / activity
- **[documented]** 484 open issues; latest release **v0.9.7 on 2025-08-15** with no release in the ~14 months to
  Oct 2026; commit stream since late 2025 is CI/security/dependency/small-fix only, with no new models.
- **[primary-issue]** Long-lived open bugs: **#1927** (parallel `ParallelExt`, 2025-05 → still open), **#1175**
  (`Alpha158.__init__` raising `TypeError: control character 'delimiter' cannot be a newline`, opened 2022-07, 7
  comments, still open), **#184** (online client `BadNamespaceError`, opened 2021-01, 12 comments, still open),
  **#235** (misleading "clear your Redis cache" error, opened 2021-01, 15 comments, still open — the single
  most-commented open issue), **#323** (distributed-training support question, opened 2021-03, still open).
- **[assessment]** The profile is "maintained by a small team as a research artefact, with community contributions for
  fixes", not "product with an SLA". Plan for **pinning a version and owning your own forks/patches**, especially if you
  depend on the parallel data path or online mode.

### 5.4 Complexity / steep learning curve
- **[documented]** The docs concede the framework diagram *"may be intimidating for new users"*.
- **[documented]** Configuration is YAML with anchor/inheritance tricks (the workspace's own config uses
  `<<: *data_handler_config` and `&`/`*` anchors), driven by `qrun`; the same doc set offers a code-based path
  (`workflow_by_code.ipynb`) precisely because "the automatic workflow may not suit the research workflow of all
  researchers".
- **[documented]** Documentation drift: readthedocs "latest" builds as **0.9.8.dev11** while the newest release is
  **0.9.7** — so the docs describe unreleased `main` behaviour. And the `data_collector` README lists 5 collectors while
  the directory has 10.

### 5.5 Technical / dependency issues
- **[documented]** **pandas break**: README carries an explicit "Break change" section — pandas 1.5→2.0 changed
  `groupby(group_keys=...)` default, which makes qlib error; qlib sets `group_keys=False` but **does not guarantee**
  correctness in `examples/rl_order_execution/scripts/gen_training_orders.py`, `examples/benchmarks/TRA/src/dataset.MTSDatasetH.py`,
  and `examples/benchmarks/TFT/tft.py`.
- **[documented]** `pandas>=1.1` with a comment that qlib had to update `fillna` (PR #1987) after pandas 2.1 deprecated
  the `method` parameter.
- **[documented]** **TFT requires `tensorflow==1.15.0` → Python 3.6–3.7 only**, which is incompatible with qlib's own
  supported Python range (3.8–3.12). The benchmark zoo is therefore not uniformly runnable.
- **[documented]** Mac M1 needs `brew install libomp` for LightGBM; #2308 adds grpcio/macOS-ARM64 build workarounds.
- **[documented]** Pins for `fastjsonschema<2.22` (py<3.10), `osqp==1.0.5` (win32+py3.8), `filelock>=3.16.0,<3.30`
  ("filelock 3.30 rejects forks while another thread changes lock descriptor ownership").
- **[primary-issue]** #1927: parallel path broken with joblib 1.5.0 (`_backend_args` vs `_backend_kwargs`).
- **[assessment]** The Cython `rolling`/`expanding` extensions must compile at install time (see `setup.py`), which is
  where many "ModuleNotFoundError: qlib.data._libs.rolling" failures (documented in the FAQ) originate.

### 5.6 MLflow coupling
- **[documented]** The recorder's only concrete experiment manager is **`MLflowExpManager`**, and the docs present
  `mlflow ui` as *the* way to inspect results. MLflow is therefore a hard dependency of the workflow/recording path
  (it is in core `dependencies`, not an extra).
- **[documented]** **`mlflow<3.13`** is pinned on `main` with the comment: *"Qlib's filesystem tracking backend is
  disabled by default in MLflow 3.13."* This is concrete evidence that an upstream MLflow release broke qlib and that
  qlib's recorder is coupled to MLflow internals/behaviour.
- **[assessment]** If your platform already has experiment tracking (MLflow, W&B, or a home-grown registry), qlib's
  recorder will either duplicate it or force MLflow into your stack. The recorder is separable in principle (it is a
  `R` API wrapper) but you inherit the MLflow dependency if you use `qrun`'s `record` section.

### 5.7 GPU / deep-model dependencies
- **[documented]** `pyproject.toml` `rl` extra = `tianshou<=0.4.10`, `torch`, **`numpy<2.0.0`**. So **using qlib's RL
  stack forces numpy < 2**, which will conflict with a modern numpy-2 environment. This is the sharpest dependency trap
  for a new platform.
- **[documented]** `analysis` extra pins `plotly<7` (*"Qlib still imports figure_factory.create_distplot, removed in
  Plotly 7"*); `docs` extra pins `scipy<=1.15.3` and `snowballstemmer<3.0`; `client` extra pins `python-socketio<6`.
- **[assessment]** None of the supervised models *require* a GPU (LightGBM/XGBoost/CatBoost are CPU); GPU only matters
  for the PyTorch deep models and RL. For an A-share platform starting with GBDT + Alpha158, GPU is optional.

### 5.8 Backtest realism limits (the most important section for A-share)
Documented as *provided*:
- **[documented]** `limit_threshold` for China (0.095 in `strategy.rst`; 0.099 in `data.rst`'s mode table — the two docs
  disagree), `trade_unit = 100` (A-share lot), T+1-aware **label** convention, and suspension → NaN data convention.
- **[documented]** v0.9.1 fixed limit-up/limit-down order handling in the topk strategy (#1407: *"allow sell in
  limit-up case and allow buy in limit-down case"*) — i.e. this area has had correctness bugs.
Documented as *not addressed* (I found no documentation for these):
- **T+1 sell restriction enforcement** in the exchange/simulator (only the label encodes T+1).
- **Order cancellation / unfilled orders on suspension (停牌)**.
- **Capacity, market impact, or slippage beyond fixed per-trade costs.**
- **Short selling / margin (融券) constraints**, ST-stock rules, or 涨跌幅 differences (10% main board vs 20% ChiNext/STAR,
  30% BSE, 5% ST) — a single scalar `limit_threshold` cannot express these.
- **Index constituent point-in-time membership** for pools like CSI300/CSI500 (the collectors download current index
  weights; survivorship in the pool is a known general pitfall).
- **[assessment]** Therefore: qlib's backtest is a good **research-grade simulator** with A-share-aware *defaults*, but an
  A-share platform must add: per-board price limits, ST handling, T+1 sell lock, suspension/order-cancel logic,
  capacity/impact, and PIT index membership. Budget for this explicitly; do not assume qlib covers it. (These are
  code-verifiable claims — I did not read `qlib/backtest/exchange.py` — so treat them as "unverified, must-check".)

### 5.9 Security (a notable, recent theme)
- **[primary-issue]** Four pickle-related security commits in ~3 months: `#2072` (fix missing deps and unsafe pickle
  usage, 2025-12-18), `#2076` (restrict pickle deserialization to safe classes, 2025-12-30), `#2099` (address reported
  unsafe `pickle.load` usages, 2026-01-28), `#2153` (use `RestrictedUnpickler` in `load_instance`, 2026-03-10).
- **[documented]** The recorder docs already flag pickle as a Known Limitation.
- **[assessment]** qlib's artifact/recorder layer loads pickles (models, datasets). The upstream hardening is welcome,
  but if you ingest **third-party** qlib artifacts or recorder URIs, treat deserialization as a trust boundary.

### 5.10 Documentation gaps
- **[documented]** Docs describe unreleased `main` (0.9.8.dev11) while the release is 0.9.7.
- **[documented]** `data_collector/README.md` is stale (lists 5 of 10 collectors; omits `crowd_source`, `baostock_5min`,
  `crypto`, `br_index`, `pit`).
- **[documented]** Internal inconsistency: China `limit_threshold` is 0.095 in `strategy.rst` vs 0.099 in `data.rst`.
- **[assessment]** The `docs/` tree is good for *concepts* (data, workflow, nested execution, PIT, RL) but thin on
  *operational* guidance (production deployment, A-share edge cases, capacity). The "Known Limitations" section of the
  recorder docs is a single sentence.

---

## 6. What gets reused WITHOUT adopting the whole framework

**The strongest documented enabler:** qlib states repeatedly that components are *"designed as loose-coupled modules,
and each component could be used stand-alone"* ([README](https://github.com/microsoft/qlib/blob/main/README.md),
[introduction.rst](https://qlib.readthedocs.io/en/latest/introduction/introduction.html)), and `strategy.rst` repeats it
for portfolio strategy: *"Because the components in Qlib are designed in a loosely-coupled way, Portfolio Strategy can
be used as an independent module also."*

**First-hand evidence from this workspace.** The local platform (`lquant`) already performs exactly the "partial reuse"
pattern, which makes it a concrete, verifiable case study rather than a blog claim:

| qlib part | What lquant did | Evidence |
|---|---|---|
| **Alpha158 formulas** | Reimplemented **all 158 factors in pure Polars/NumPy**, with formulas "逐条对照" (cross-checked line by line) against `qlib/contrib/data/loader.py::Alpha158DL`. Documents its deviations: `$vwap` proxied as `amount/volume`; `Slope`/`Rsquare`/`Resi` via numpy sliding-window closed forms; `IdxMax`/`IdxMin` positions normalised by window. | [`src/lquant/factors/qlib_alpha.py`](../../../../../../src/lquant/factors/qlib_alpha.py) |
| **qlib binary data format** | Reimplemented the `.bin` writer (`calendars/day.txt`, `instruments/all.txt`, `features/<SYM>/<field>.day.bin`, float32 LE, **first element = calendar start index**, NaN for gaps) instead of calling `dump_bin.py`. | [`src/lquant/qlib_io/export.py`](../../../../../../src/lquant/qlib_io/export.py) |
| **Workflow + backtest + strategy** | Reuses qlib's own `qrun`-compatible YAML with `Alpha158` handler, `LGBModel`, `TopkDropoutStrategy`, and `exchange_kwargs` (`deal_price: close`, `open_cost 0.0005`, `close_cost 0.0015`, `min_cost 5`, `trade_unit 100`). | [`config/qlib/workflow_alpha158_lgbm.yaml`](../../../../../../config/qlib/workflow_alpha158_lgbm.yaml) |
| **Recorder** | Has a local `data/qlib/qlib_runs.db` + `runs/` directory, i.e. it uses the recorder/MLflow-backed store locally. | `data/qlib/` in this workspace |

This is direct evidence that the **two most-copied assets are (1) the Alpha158 formula list and (2) the `.bin`
data layout** — both of which are *text/spec*, not runtime dependencies. It also shows the pattern of **reimplementing
the data plane in a modern columnar engine (Polars)** while still *running* qlib for model/backtest.

**Component-by-component self-containment assessment:**

1. **Alpha158 / Alpha360 definitions — HIGH reuse, near-zero coupling.** The formulas are plain expression strings
   returned by `Alpha158DL.get_feature_config()` / `Alpha360DL.get_feature_config()`; they are copyable text. There is
   no runtime dependency on qlib to *transcribe* them. **[verified from source + local reimplementation]**
2. **Expression engine — MEDIUM reuse, needs the data plane.** Usable through `QlibDataLoader`/`D.features` with your
   own fields, and operators are user-registrable (`tests/test_register_ops.py`), but it needs `qlib.init(...)` and the
   calendar/instruments/`.bin` layout. Reusing it means adopting qlib's storage contract. The local project chose
   **not** to reuse it (Polars instead) while still reusing the *formulas* — a clean demonstration of the trade-off.
   **[verified from docs; the local choice is first-hand]**
3. **Backtest / nested executor / strategies — MEDIUM-HIGH reuse.** `backtest_daily(pred_score, strategy=..., ...)`
   plus `risk_analysis` is documented as usable from a plain `pred_score` DataFrame indexed by
   `(datetime, instrument)` — i.e. **you can feed your own model's output without using qlib's model layer at all.**
   `WeightStrategyBase` is designed for "target weights only" custom strategies. The nested executor is the
   distinctive piece worth adopting if you need multi-level (daily + intraday) joint backtest. **[verified from
   `strategy.rst` + local config]**
4. **Recorder / MLflow — LOW-MEDIUM reuse.** The `R` API is usable independently, but it pulls in MLflow (core
   dependency, `mlflow<3.13`) and pickles artifacts. Reasonable to reuse only if you want MLflow; otherwise it is
   duplication. **[verified from `recorder.rst` + `pyproject.toml`]**
5. **DDG-DA meta-learning — LOW reuse as a component; MEDIUM as a pattern.** Shipped as an example under
   `examples/benchmarks_dynamic/DDG-DA` with a `MetaTask`/`MetaTaskDataset`/`MetaModel` framework. The *idea*
   (reweight training windows to handle concept drift) is portable; the implementation is research code tied to qlib's
   dataset/segment abstractions. **[verified from README + docs; reuse-effort is my assessment]**
6. **RL order execution — LOW reuse unless you are doing execution research.** `qlib/rl` is a coherent framework
   (Interpreter/Simulator/Trainer + `order_execution`) and supports nesting, but it pins `tianshou<=0.4.10` and
   `numpy<2.0.0`, and needs high-frequency data. **[verified from `pyproject.toml` + API reference]**
7. **`.bin` format + expression/dataset cache as a pure storage layer — MEDIUM reuse, HIGH lock-in.** The format is
   simple and well-specified (§3.5) and the performance claim is large (7.4 s vs 184–368 s). But adopting it means your
   data plane is qlib's, and the local project's choice to reimplement the writer rather than depend on it suggests the
   lock-in is avoidable. **[verified from docs + local reimplementation]**

**Honest gap:** I did **not** collect third-party blog/fork evidence specifically about extracting Alpha158 or running
qlib's backtest standalone. The evidence above is (a) qlib's own documented stand-alone claims, (b) source inspection
showing the formulas are plain strings, and (c) this workspace's first-hand reimplementation. That is strong, but it is
not a survey of community practice — mark it as such.

---

## 7. Version / feature notes: 0.9.x vs current `main`

### 7.1 Version facts

- **PyPI latest = `0.9.7`**, `requires_python >=3.8.0`, classifier `Development Status :: 3 - Alpha`
  ([PyPI JSON](https://pypi.org/pypi/pyqlib/json)). The local install is 0.9.7.
- **Newest GitHub release = `v0.9.7`, published 2025-08-15** ([releases API](https://api.github.com/repos/microsoft/qlib/releases)).
- **`main` docs build as `0.9.8.dev11`** ([readthedocs](https://qlib.readthedocs.io/en/latest/)) — i.e. `main` carries
  unreleased work, and `pyproject.toml` uses `setuptools-scm` with `version_scheme = "guess-next-dev"`, consistent with
  that.
- Supported Python: **3.8 – 3.12** (README table + classifiers).

### 7.2 What 0.9.x added (from release bodies)

- **v0.9.7 (2025-08-15)** — the most feature-rich recent release: **Parquet data support** (#1966);
  **`pydantic-settings` for MLflow config** + dependency updates (#1962); **`BaseDataHandler` + unified fetch
  interface** (#1958); geometric accumulation mode for `risk_analysis` (#1938); a util to auto-derive horizon (#1509);
  `general_nn` adapted for `rdagent_qlib` (#1928); **Data Health Checker** (#1574). Fixes: LightGBM install on macOS
  (#1980), `group_keys=False` in Average Ensemble (#1913), pkl loading in `StaticDataLoader` (#1896), CSI300
  constituents URL (#1883), SBBStrategyEMA empty-price case (#1677), `fillna` bug (#1914), col-name error on fetch
  (#1904). Docs: chenditc links updated to always point at the latest release (#1877).
- **v0.9.6 (2024-12-23)** — **Nested data loader** (#1822), more dataloader examples (#1823), PTNN datatype/alignment
  tests (#1827); fixes for async call, duplicate logging, Yahoo daily format inconsistency (#1517), TSDataSampler
  slicing (#1803), invalid-data normalisation panic (#1698), HS_SYMBOLS_URL 404 (#1758).
- **v0.9.5 (2024-05-24)** — small: `cn_index` requirements fix, get-data error, reading string NA as NaN (#1736).
- **v0.9.4 (2024-05-07)** — exploration noise for RL training (#1481), **multi-pass portfolio analysis record** (#1546),
  **`add_baostock_collector` (#1641)**, orderbook data download (#1754), suppress `SettingWithCopyWarning`.
- **v0.9.3 (2023-07-18)** — rolling API adjustment (#1594), macOS pyqlib version fix (#1605).
- **v0.9.2 (2023-06-25)** — **order execution open-sourced (#1447)**, **DDG-DA refined (#1472)**, **RL backtest
  pipeline on 5-min data (#1417)**, KRNN & Sandwich models documented (#1414), Redis password support (#1508), TCN
  input-dim fix (#1520).
- **v0.9.1 (2023-01-29)** — **limit-up/limit-down order handling fix in topk strategy (#1407)**, RL training pipeline on
  5-min data (#1415), plot enhancements, ZScoreNorm fix (#1398).
- **v0.9.0 (2022-12-09)** — the 0.9 series baseline.

**Reading of the 0.9.x line:** the last *substantive* capability releases were 0.9.2/0.9.4 (RL order execution, DDG-DA,
baostock collector, 5-min RL pipeline). **0.9.7 is mostly platform hygiene** (Parquet, config/dependency
modernisation, refactored handler base class, data health checker) rather than new alpha capability.

### 7.3 What `main` has beyond 0.9.7 (from the commit log)

Grouped by theme:

- **China data plumbing (most relevant to you):** AkShare-based unified trade calendar (#2093), Baostock calendar
  replacing Eastmoney (#2193), `FileCalendarStorage` index fix (#2195), daily date-format fix (#2015), lowercase
  features-directory check (#2087).
- **Security (pickle):** #2072, #2076, #2099, #2153 (§5.9).
- **Config robustness:** explicit validation for required configuration fields (#2078) — with a documented "future
  migration plan" for config semantics.
- **Correctness:** backtest calendar overflow when `end_time` missing (#2127), SoftTopkStrategy deterministic budget
  allocation (#2077), PyTorch scheduler semantic-version comparison (#2094), US symbols URL (#1975), SeriesDFilter
  (#2051), `handler_mod` with `None` end date (#2068).
- **Build/CI:** dependency-compatibility fixes incl. grpcio on older macOS ARM64 (#2308), Actions pinned to SHAs (#2318).

### 7.4 Dependency posture on `main` (`pyproject.toml`) — read this before pinning

Core: `pyyaml, numpy, pandas>=1.1, mlflow<3.13, filelock>=3.16.0,<3.30, redis, dill, fire, ruamel.yaml>=0.17.38,
python-redis-lock, tqdm, pymongo, loguru, lightgbm, gym, cvxpy, joblib, matplotlib, jupyter, nbconvert, pyarrow,
pydantic-settings, setuptools-scm`; plus `osqp==1.0.5` on win32/py3.8 and `fastjsonschema<2.22` on py<3.10.
Extras: `rl` = `tianshou<=0.4.10, torch, numpy<2.0.0`; `analysis` = `plotly<7, statsmodels`; `client` =
`python-socketio<6, tables`; `test` = `baostock, yahooquery, lxml<6.1.3`; `docs` = `scipy<=1.15.3, sphinx, ..., snowballstemmer<3.0`.
Entry point: `qrun = qlib.cli.run:run`.
**Key consequences:** (a) core supports modern numpy/pandas, but **the RL extra forbids numpy 2.x**; (b) **MLflow is a
core dependency and is pinned below 3.13**; (c) several pins exist only to route around upstream breakage.

### 7.5 Coming soon / watch list

- **BPQP for end-to-end learning** — README lists it as *"📈 Coming soon! (Under review)"* with
  [PR #1863](https://github.com/microsoft/qlib/pull/1863). Not in 0.9.7; not landed on `main` in the commits I
  retrieved.
- **RD-Agent integration** — already the README's headline feature; the forward-looking bet is *LLM-driven automated
  factor mining / model optimisation on top of qlib*, not qlib itself gaining new models.
- **China data sources** — the AkShare/Baostock calendar work suggests qlib is slowly reducing its Yahoo dependence for
  CN. Worth tracking if you plan to use qlib's own collectors rather than your own data plane.
- **qlib-server (online mode)** — separate repo; unchanged in the commits I saw; still the least-trodden path (open bug
  #184).

---

## 8. Decision-relevant implications for a Chinese A-share quant platform

*These are my synthesis, not documented facts.*

1. **Adopt the specs, not (necessarily) the runtime.** The two assets with the best reuse-to-cost ratio are the
   **Alpha158/Alpha360 formula definitions** and the **`.bin` data layout** — both copyable. This workspace already
   proves the pattern (Polars reimplementation + custom `.bin` writer) while still *running* qlib for
   model+backtest. That hybrid is the low-risk default.
2. **The data plane is the real cost, and qlib does not solve it for A-shares.** The official dataset is disabled;
   the community source needs a Tushare token, is single-maintainer, and has unclear commercial licensing for its
   Wind/Caihui-derived history. Expect to own the A-share data plane (Tushare/AkShare/Baostock/your vendor →
   adjusted OHLCV + `factor` + extra fields → qlib `.bin`). The `dump_bin.py` column contract and
   `check_data_health.py` are worth reusing as a *specification*.
3. **Reuse the backtest + strategy layer selectively.** `TopkDropoutStrategy` + `backtest_daily` + `risk_analysis`
   accepts your own `pred_score` DataFrame, so you can adopt qlib's portfolio/backtest semantics without adopting its
   model or data layers. The **nested executor** is the genuinely differentiated piece if you need daily+intraday joint
   backtest.
4. **Budget for A-share backtest realism that qlib only partly provides.** Provided: limit threshold, 100-share lot,
   T+1-aware label, suspension→NaN. Not documented (must verify in code / likely add): T+1 sell lock, per-board limits
   (10%/20%/30%/5% ST), 停牌 order cancellation, capacity/market impact, PIT index membership, short/margin rules.
   Also note the `limit_threshold` 0.095-vs-0.099 doc inconsistency.
5. **Pin versions and own your patches.** With ~1 release/year, 484 open issues, and long-lived open bugs on the
   parallel data path (#1927) and online mode (#184), treat qlib as a **vendored research dependency**, not a service.
   If you use the RL stack, you are locked to `numpy<2`.
6. **Decide about MLflow early.** It is a core dependency of the recorder and pinned `mlflow<3.13` because MLflow 3.13
   disabled qlib's filesystem tracking backend. If your platform already has experiment tracking, either skip qlib's
   recorder or accept MLflow.
7. **Watch RD-Agent, not qlib-core, for new alpha capability.** qlib-core is in maintenance-with-hygiene mode; the
   innovation has moved to `microsoft/RD-Agent` (LLM-driven factor/model R&D) and to the community data plane.
8. **Do not cite TradingAgents/FinGPT as qlib integrations.** They are adjacent LLM/FinLLM projects with no verified
   qlib dependency.

---

## 9. Source list

**Primary — qlib repo & docs**
- [microsoft/qlib README](https://github.com/microsoft/qlib/blob/main/README.md) · [raw](https://raw.githubusercontent.com/microsoft/qlib/main/README.md)
- [docs/introduction/introduction.rst](https://qlib.readthedocs.io/en/latest/introduction/introduction.html)
- [docs/component/data.rst (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/data.rst)
- [docs/component/strategy.rst (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/strategy.rst)
- [docs/component/recorder.rst (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/docs/component/recorder.rst)
- [docs/component/highfreq.html](https://qlib.readthedocs.io/en/latest/component/highfreq.html)
- [docs/advanced/PIT.html](https://qlib.readthedocs.io/en/latest/advanced/PIT.html)
- [docs/advanced/alpha.rst (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/docs/advanced/alpha.rst)
- [docs/component/meta.html](https://qlib.readthedocs.io/en/latest/component/meta.html) · [docs/component/online.html](https://qlib.readthedocs.io/en/latest/component/online.html) · [docs/component/report.html](https://qlib.readthedocs.io/en/latest/component/report.html)
- [API reference](https://qlib.readthedocs.io/en/latest/reference/api.html)
- [qlib/contrib/data/handler.py (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/qlib/contrib/data/handler.py) · [qlib/contrib/data/loader.py (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/qlib/contrib/data/loader.py)
- [scripts/data_collector/README.md (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/README.md) · [contents API](https://api.github.com/repos/microsoft/qlib/contents/scripts/data_collector)
- [scripts/data_collector/crowd_source/README.md (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/crowd_source/README.md)
- [scripts/data_collector/baostock_5min/README.md (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/scripts/data_collector/baostock_5min/README.md)
- [pyproject.toml (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/pyproject.toml) · [setup.py (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/setup.py)
- [docs/_static/img/framework.svg (raw)](https://raw.githubusercontent.com/microsoft/qlib/main/docs/_static/img/framework.svg)

**Primary — paper, package, API metadata**
- [Qlib paper, arXiv:2009.11189](https://arxiv.org/abs/2009.11189) · [HTML full text](https://arxiv.org/html/2009.11189v1)
- [PyPI pyqlib JSON](https://pypi.org/pypi/pyqlib/json) · [PyPI project page](https://pypi.org/project/pyqlib/)
- [GitHub repo metadata](https://api.github.com/repos/microsoft/qlib) · [releases](https://api.github.com/repos/microsoft/qlib/releases) · [commits](https://api.github.com/repos/microsoft/qlib/commits?per_page=40) · [open issues by comments](https://api.github.com/search/issues?q=repo:microsoft/qlib+is:issue+state:open&sort=comments&order=desc)

**Primary — ecosystem repos**
- [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) · [R&D-Agent-Quant paper, arXiv:2505.15155](https://arxiv.org/abs/2505.15155)
- [microsoft/qlib-server](https://github.com/microsoft/qlib-server)
- [chenditc/investment_data README (raw)](https://raw.githubusercontent.com/chenditc/investment_data/main/README.md) · [releases](https://github.com/chenditc/investment_data/releases)
- [TauricResearch/TradingAgents README (raw)](https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md) · [TradingAgents paper, arXiv:2412.20138](https://arxiv.org/abs/2412.20138)
- [AI4Finance-Foundation/FinGPT README (raw)](https://raw.githubusercontent.com/AI4Finance-Foundation/FinGPT/master/README.md)

**Specific issues / commits cited**
- [#1927 ParallelExt `_backend_args`](https://github.com/microsoft/qlib/issues/1927) · [#1175 Alpha158 control-character TypeError](https://github.com/microsoft/qlib/issues/1175) · [#184 online client BadNamespaceError](https://github.com/microsoft/qlib/issues/184) · [#235 Redis-cache error message](https://github.com/microsoft/qlib/issues/235) · [#323 distributed training](https://github.com/microsoft/qlib/issues/323) · [#991 price-adjustment discussion](https://github.com/microsoft/qlib/issues/991#issuecomment-1075252402) · [#1863 BPQP (under review)](https://github.com/microsoft/qlib/pull/1863)
- China-data commits: [#2093 akshare calendar](https://github.com/microsoft/qlib/pull/2093) · [#2193 baostock calendar](https://github.com/microsoft/qlib/pull/2193) · [#2195 FileCalendarStorage](https://github.com/microsoft/qlib/pull/2195) · [#2015 date format](https://github.com/microsoft/qlib/pull/2015) · [#2087 lowercase features dirs](https://github.com/microsoft/qlib/pull/2087)
- Pickle-security commits: [#2072](https://github.com/microsoft/qlib/pull/2072) · [#2076](https://github.com/microsoft/qlib/pull/2076) · [#2099](https://github.com/microsoft/qlib/pull/2099) · [#2153](https://github.com/microsoft/qlib/pull/2153)
- Dependency/CI commits: [#2308 dependency compatibility](https://github.com/microsoft/qlib/pull/2308) · [#2318 pin Actions](https://github.com/microsoft/qlib/pull/2318) · [#2078 config validation](https://github.com/microsoft/qlib/pull/2078)

**Workspace (first-hand evidence for §6)**
- [`src/lquant/factors/qlib_alpha.py`](../../../../../../src/lquant/factors/qlib_alpha.py) · [`src/lquant/qlib_io/export.py`](../../../../../../src/lquant/qlib_io/export.py) · [`config/qlib/workflow_alpha158_lgbm.yaml`](../../../../../../config/qlib/workflow_alpha158_lgbm.yaml)

**Secondary / unverified (flagged, not relied upon)**
- Chinese-language comparative and critique articles surfaced in search (a CSDN piece on Qlib/AkShare/Tushare/Baostock benchmarking; a CSDN piece titled "Qlib+backtrader 回测到实盘：那些被忽略的致命细节"; a Zhihu article on RD-Agent's strategy-R&D pipeline). I did **not** fetch these, so no claim in this report rests on them.
- Third-party auto-generated docs (e.g. DeepWiki pages for `microsoft/qlib`) appeared in search results; not used as a source of record.
