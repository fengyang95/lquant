# qlib 0.9.7 Capability Inventory

**Subject:** `microsoft/qlib` (pyqlib) **0.9.7**, as installed in the read-only tree
`/Users/lyp/code/lquant/.venv-qlib/lib/python3.12/site-packages/qlib`

**Purpose:** evidence-backed capability inventory to support an integration decision for
`lquant` (a Chinese A-share quant platform).

**Method:** every claim below was read from the installed source. Line numbers are
`path/file.py:LINE` relative to the qlib package root. Where behaviour is
non-obvious, the relevant docstring is quoted. Nothing here is inferred from
qlib's public documentation or from memory.

---

## 0. Scope, packaging reality, and runtime environment

### 0.1 What the wheel actually contains

| Metric | Value |
|---|---|
| Python files (excluding `__pycache__`) | 230 |
| Cython sources | 2 (`data/_libs/rolling.pyx`, `data/_libs/expanding.pyx`) |
| Compiled extensions shipped | 2 (`rolling.cpython-312-darwin.so`, `expanding.cpython-312-darwin.so`) |
| YAML config files shipped in the package | **0** |
| Files under `qlib/tests/` | **3** (`__init__.py`, `config.py`, `data.py`) |
| Total LOC | 55,985 |

Two packaging facts matter a lot for planning:

1. **No benchmark/handler YAML ships in the wheel.** All the
   `workflow_config_*_Alpha158_*.yaml` files referenced by the docs live in the
   GitHub repo under `examples/benchmarks/`, not in the installed package. A
   platform integrating qlib must author its own task YAML.
2. **No test suite ships in the wheel.** `qlib/tests/__init__.py` (12 KB) contains
   *reusable test base classes* — `TestAutoData` (`qlib/tests/__init__.py:16`),
   `TestOperatorData` (`:64`), `MockStorageBase` (`:165`), `MockCalendarStorage`
   (`:170`), `MockInstrumentStorage` (`:186`), `MockFeatureStorage` (`:207`),
   `TestMockData` (`:267`) — plus `qlib/tests/data.py:18 GetData` (a downloader)
   and `qlib/tests/config.py` (`get_data_handler_config:53`, `get_dataset_config:69`,
   `get_gbdt_task:94`, `get_record_lgb_config:101`, `get_record_xgboost_config:112`).
   The actual test cases are in the repo. "Is it tested?" therefore cannot be
   answered from the wheel alone; treat core-vs-contrib placement and docstring
   caveats as the available maturity signals.

### 0.2 Dependency availability in this venv

Verified with `importlib.util.find_spec`:

| Package | Installed | Consequence |
|---|---|---|
| numpy, pandas, scipy, scikit-learn | ✅ | core data/ops/risk models work |
| lightgbm | ✅ | `LGBModel`, `DEnsembleModel`, `HFLGBModel` usable |
| mlflow | ✅ | workflow experiment tracking usable |
| cvxpy | ✅ | `EnhancedIndexingOptimizer` usable |
| gym (legacy) | ✅ | RL env plumbing partially importable |
| redis, filelock, ruamel.yaml, joblib, pyarrow, fire, tqdm, matplotlib | ✅ | caching, config, CLI, plotting (matplotlib only) |
| **torch** | ❌ | **all 27 PyTorch models, RL, DDG-DA meta-learning unusable** |
| **tensorflow** | ❌ | no TF models exist anyway (see §4) |
| **xgboost** | ❌ | `XGBModel` unusable |
| **catboost** | ❌ | `CatBoostModel` unusable |
| **tianshou** | ❌ | `qlib/rl` unusable |
| **plotly** | ❌ | `qlib/contrib/report` plotting unusable |
| **statsmodels, seaborn** | ❌ | `analysis_model_performance` / `report.data.ana` unusable |
| **hyperopt, optuna, ray** | ❌ | `qlib/contrib/tuner` unusable |
| numba, dask, polars | ❌ | not used by qlib anyway |

> **Bottom line:** as installed, qlib's usable surface is *linear/GBDT models +
> the full data/backtest/strategy/risk-model/optimizer stack + MLflow workflow*.
> The entire deep-learning zoo, RL, meta-learning, plotting and hyper-parameter
> tuning require additional installs.

---

## 1. Data layer — `qlib/data/`

### 1.1 What it provides

A four-layer data stack: **providers** (abstract + local + client) → **expression
engine** → **caching** → **dataset/handler**. All four are core (`qlib/`), not contrib.

### 1.2 Global entry points

| Entry point | Location | Notes |
|---|---|---|
| `D` (main facade) | `qlib/data/data.py:1289` | `D: BaseProviderWrapper = Wrapper()` |
| `Cal` | `qlib/data/data.py:1283` | calendar provider wrapper |
| `Inst` | `qlib/data/data.py:1284` | instrument provider wrapper |
| `FeatureD` | `qlib/data/data.py:1285` | raw feature provider |
| `PITD` | `qlib/data/data.py:1286` | point-in-time provider (**not** exported in `qlib/data/__init__.py`) |
| `ExpressionD` | `qlib/data/data.py:1287` | expression provider |
| `DatasetD` | `qlib/data/data.py:1288` | dataset provider |
| `D.features(...)` | `qlib/data/data.py:1162` | `BaseProvider.features`; returns a `(instrument, datetime)` MultiIndex DataFrame |
| `D.list_instruments(...)` | `qlib/data/data.py:1159` | delegates to `Inst.list_instruments` |
| `D.instruments(...)` | `qlib/data/data.py:1151` | market + `filter_pipe` |
| `D.calendar(...)` | `qlib/data/data.py:1148` | trading calendar |
| Wrapper registration | `qlib/data/data.py:1292 register_all_wrappers` | wires config → provider instances at `qlib.init()` |

`D.features` accepts `disk_cache` in {0 skip, 1 use, 2 replace} and falls back to a
provider without the `disk_cache` kwarg on `TypeError`
(`qlib/data/data.py:1162-1191`).

### 1.3 Provider abstraction (the data-source extension seam)

| ABC | Location | Abstract method |
|---|---|---|
| `ProviderBackendMixin` | `qlib/data/data.py:43` | `get_default_backend:49`, `backend_obj:58` |
| `CalendarProvider` | `qlib/data/data.py:65` | `calendar:71`, `load_calendar:182`, `locate_index:111` |
| `InstrumentProvider` | `qlib/data/data.py:199` | `instruments:206`, `list_instruments:267` |
| `FeatureProvider` | `qlib/data/data.py:307` | `feature:314` |
| `PITProvider` | `qlib/data/data.py:338` | `period_feature:340` |
| `ExpressionProvider` | `qlib/data/data.py:383` | `expression:410`; caches parsed expressions (`:389-392`) |
| `DatasetProvider` | `qlib/data/data.py:446` | `dataset:453` |

Concrete local implementations: `LocalCalendarProvider:637`,
`LocalInstrumentProvider:678`, `LocalFeatureProvider:726`, `LocalPITProvider:744`,
`LocalExpressionProvider:833`, `LocalDatasetProvider:882`. Client/server variants:
`ClientCalendarProvider:961`, `ClientInstrumentProvider:985`,
`ClientDatasetProvider:1027`, plus the `Client` RPC shim
(`qlib/data/client.py:16`, `connect_server:35`, `send_request:49`).

Facades: `BaseProvider:1140`, `LocalProvider:1193` (adds
`features_uri:1209` / `_uri:1194`), `ClientProvider:1224`.

**Point-in-time (PIT).** `PITProvider.period_feature` returns a *period-indexed*
series; the docstring specifies the index is integers such as `202001`
(`qlib/data/data.py:340-378`). PIT requires per-instrument *record* files, not
`.bin` features; record dtype/nan sentinels are configured at
`qlib/config.py:226-237` (`pit_record_type`, `pit_record_nan`). PIT is **not**
reachable through `D.features` — you must use the `P` operator, and qlib raises a
directive error if you try otherwise:

```
qlib/data/data.py:747-750
"Expected pd.Timestamp for `cur_time`, got ... Advices: you can't query PIT data
 directly(e.g. '$$roewa_q'), you must use `P` operator to convert data to each day
 (e.g. 'P($$roewa_q)')"
```

Also flagged in-source as not thread-safe:
`qlib/data/data.py:745` — `# NOTE: This class is not multi-threading-safe!!!!`
and `:744` — `# TODO: Add PIT backend file storage`.

### 1.4 Expression / operator engine — `qlib/data/ops.py`

Base classes: `qlib/data/base.py:13 Expression`, `:238 Feature`, `:266 PFeature`,
`:276 ExpressionOps`. `Expression` implements the full Python operator protocol
(`__gt__:32` … `__ror__:137`) so expressions compose algebraically.
`Expression.load:142` is the shared driver: it consults the global memory cache
`H["f"]` keyed by `(str(expr), instrument, start_index, end_index, *args)`
(`:188-190`), calls `_load_internal`, and caches the result.

**Operator inventory (all in `qlib/data/ops.py`):**

| Category | Operators (line) |
|---|---|
| Element-wise | `ElemOperator:37`, `ChangeInstrument:64`, `NpElemOperator:97`, `Abs:122`, `Sign:140`, `Log:167`, `Mask:185`, `Not:212` |
| Binary | `PairOperator:231`, `NpPairOperator:279`, `Power:338`, `Add:358`, `Sub:378`, `Mul:398`, `Div:418`, `Greater:438`, `Less:458`, `Gt:478`, `Ge:498`, `Lt:518`, `Le:538`, `Eq:558`, `Ne:578`, `And:598`, `Or:618` |
| Conditional | `If:639` |
| Rolling (unary) | `Rolling:713`, `Ref:781`, `Mean:827`, `Sum:847`, `Std:867`, `Var:887`, `Skew:907`, `Kurt:929`, `Max:951`, `IdxMax:971`, `Min:999`, `IdxMin:1019`, `Quantile:1047`, `Med:1079`, `Mad:1099`, `Rank:1133`, `Count:1171`, `Delta:1191`, `Slope:1221`, `Rsquare:1257`, `Resi:1286`, `WMA:1314`, `EMA:1349` |
| Rolling (binary) | `PairRolling:1387`, `Corr:1467`, `Cov:1501` |
| Resampling | `TResample:1528` |
| PIT | `P:qlib/data/pit.py:23`, `PRef:qlib/data/pit.py:62` |

`get_longest_back_rolling()` / `get_extended_window_size()` are implemented per
operator (e.g. `Rolling:757`/`:764`, `If:673`/`:690`) and are what let qlib fetch
exactly enough lookback to evaluate a rolling expression.

**Cython acceleration.** `Slope`, `Rsquare`, `Resi` and their expanding
counterparts call compiled kernels:

| Kernel | Location |
|---|---|
| `rolling_mean`, `rolling_slope`, `rolling_rsquare`, `rolling_resi` | `qlib/data/_libs/rolling.pyx:193,197,201,205` |
| `expanding_slope`, `expanding_rsquare`, `expanding_resi` | `qlib/data/_libs/expanding.pyx` |

The import is wrapped in `try/except ImportError` with a re-raise
(`qlib/data/ops.py:19-27`) and a separate `except ValueError` that *disables* the
Cython ops with a printed warning (`:29-33`) — i.e. qlib degrades rather than fails
on ABI-mismatched numpy.

`scipy.stats.percentileofscore` is a hard dep of `Quantile`
(`qlib/data/ops.py:12`).

**Registration.** `OpsWrapper:1619` holds a name→class dict; `Operators` singleton
at `:1667`; `register_all_ops:1670` resets and registers `OpsList + [P, PRef]` then
appends `C.custom_ops` (`:1678-1681`). Overriding a builtin is allowed but warns
(`:1651-1654`).

### 1.5 Caching — `qlib/data/cache.py`

| Layer | Location | Backing store |
|---|---|---|
| `MemCache` (3 units: `c`alendar / `i`nstrument / `f`eature) | `qlib/data/cache.py:136`; units `MemCacheLengthUnit:120`, `MemCacheSizeofUnit:128` | process memory, LRU-ish, `set_limit_size:81` |
| Global singleton `H` | `qlib/data/cache.py:1198` | `H = MemCache()` |
| `MemCacheExpire` | `qlib/data/cache.py:180` | TTL wrapper, `C.mem_cache_expire` default 3600 s (`qlib/config.py:173`) |
| `CacheUtils` | `qlib/data/cache.py:209` | **Redis** distributed reader/writer locks (`reader_lock:257`, `writer_lock:285`) |
| `BaseProviderCache` | `qlib/data/cache.py:294` | decorator base; `check_cache_exists:305`, `clear_cache:313` |
| `ExpressionCache` | `qlib/data/cache.py:329` | abstract |
| `DiskExpressionCache` | `qlib/data/cache.py:489` | HDF5 + `.meta`/`.index` sidecars + Redis locks |
| `DatasetCache` | `qlib/data/cache.py:380` | abstract; `cache_to_origin_data:466` |
| `DiskDatasetCache` | `qlib/data/cache.py:646` | `pd.HDFStore` (`read_data_from_cache:662`) |
| `SimpleDatasetCache` | `qlib/data/cache.py:1063` | pickle under `C.local_cache_path` |
| `DatasetURICache` | `qlib/data/cache.py:1117` | in-memory keyed by cache URI |
| `CalendarCache` / `MemoryCalendarCache` | `qlib/data/cache.py:1179` / `:1183` | TTL calendar cache |

Cache-key normalisation helpers live in `qlib/utils/__init__.py`:
`hash_args:271`, `normalize_cache_fields:350`, `normalize_cache_instruments:359`.

`qlib.init(clear_mem_cache=True)` clears `H` on every init (`qlib/__init__.py`,
`init()` body). Disk cache *requires Redis*: `qlib/config.py:132` defines
`DEPENDENCY_REDIS_CACHE = (DISK_DATASET_CACHE, DISK_EXPRESSION_CACHE)` and
`qlib/config.py:464-480` silently downgrades those caches to `None` with a warning
if `can_use_cache()` fails.

### 1.6 Storage backends — `qlib/data/storage/`

Abstract layer `qlib/data/storage/storage.py`: `BaseStorage:78`,
`CalendarStorage:84`, `InstrumentStorage:191`, `FeatureStorage:255`
(`write:299`, `rebase:354`, `rewrite:443`). User-override hooks:
`UserCalendarStorage:24`, `UserInstrumentStorage:38`, `UserFeatureStorage:52`.

Concrete file backend `qlib/data/storage/file_storage.py`: `FileStorageMixin:21`,
`FileCalendarStorage:76`, `FileInstrumentStorage:192`, `FileFeatureStorage:285`.
`support_freq` is discovered by globbing `calendars/*.txt`
(`qlib/data/storage/file_storage.py:44-57`).

**On-disk format (verified in `FileFeatureStorage.write:299`):**
`features/<instrument_lower>/<field>.<freq>.bin` as **little-endian float32
(`"<f"`)**, where the **first float is the start calendar index**, followed by the
value array (`:303`, `:308-310`). `start_index` reads the first 4 bytes
(`:332-337`); `end_index = start_index + len - 1` (`:340-343`). Calendars are plain
text written with `np.savetxt(fmt="%s")` (`:122-124`). Instruments are text/JSON
(`_read_instrument:203`, `_write_instrument:220`). This is the format
`qlib.tests.data.GetData` downloads and the format any custom data source must
reproduce or replace.

### 1.7 Dataset / handler layer — `qlib/data/dataset/`

| Class | Location | Role |
|---|---|---|
| `Dataset` | `qlib/data/dataset/__init__.py:15` | abstract, `Serializable` |
| `DatasetH` | `qlib/data/dataset/__init__.py:72` | handler + `segments` (train/valid/test) |
| `TSDataSampler` | `qlib/data/dataset/__init__.py:272` | time-series sampler, `__getitem__:597`, `build_index:490` |
| `TSDatasetH` | `qlib/data/dataset/__init__.py:642` | `step_len` (default 30, `DEFAULT_STEP_LEN:661`), `_extend_slice:680` |
| `DataHandlerABC` | `qlib/data/dataset/handler.py:26` | `fetch:58` |
| `DataHandler` | `qlib/data/dataset/handler.py:68` | DataFrame-backed, `setup_data:174`, `fetch:198`, `get_range_selector:347`, `get_range_iterator:364` |
| `DataHandlerLP` | `qlib/data/dataset/handler.py:383` | three data planes: `DK_R` raw, `DK_I` infer, `DK_L` learn (`ATTR_MAP:411`); `PTYPE_I` independent / `PTYPE_A` append (`:415`, `:422`); `cast:734`, `from_df:766` |

`qlib/data/dataset/__init__.py:722` has `__all__ = ["Optional", "Dataset", "DatasetH"]`
— `TSDatasetH`/`TSDataSampler` are importable but not exported (and `Optional`
leaking into `__all__` is a small hygiene defect).

**Loaders** (`qlib/data/dataset/loader.py`): `DataLoader:18`, `DLWParser:62`,
`QlibDataLoader:153` (the expression-string loader), `StaticDataLoader:230`,
`NestedDataLoader:291`, `DataLoaderDH:350`.

**Processors** (`qlib/data/dataset/processor.py`): `Processor:35`
(`fit:36`, `__call__:49`, `is_for_infer:62`, `readonly:74`), then

| Processor | Line | Effect |
|---|---|---|
| `DropnaProcessor` | `:94` | drop NaN rows in a field group |
| `DropnaLabel` | `:105` | `is_for_infer() == False` (`:109`) — learning-only |
| `DropCol` | `:114` | drop columns |
| `FilterCol` | `:129` | keep columns |
| `TanhProcess` | `:146` | `tanh` squash |
| `ProcessInf` | `:161` | replace ±inf |
| `Fillna` | `:179` | fill with `fill_value=0` |
| `MinMaxNorm` | `:196` | fit-range min-max |
| `ZScoreNorm` | `:228` | fit-range z-score |
| `RobustZScoreNorm` | `:262` | median/MAD + optional outlier clip |
| `CSZScoreNorm` | `:300` | **cross-sectional** z-score, `method="zscore"` or `"robust"` (`:303-309`) |
| `CSRankNorm` | `:326` | cross-sectional rank → `(rank_pct - 0.5) * 3.46` (`:352-358`) |
| `CSZFillna` | `:362` | cross-sectional mean fill |
| `HashStockFormat` | `:374` | convert to hashed-stock storage format |
| `TimeRangeFlt` | `:383` | an `InstProcessor`, not a `Processor` |

**Dataset storage** (`qlib/data/dataset/storage.py`): `BaseHandlerStorage:12`,
`NaiveDFStorage:54`, `HashingStockStorage:89`.
**Utils** (`qlib/data/dataset/utils.py`): `get_level_index:12`,
`fetch_df_by_index:41`, `fetch_df_by_col:81`, `convert_index_format:92`,
`init_task_handler:119`.
**Weighting** (`qlib/data/dataset/weight.py`): `Reweighter:5` (`reweight:12`).
**Instrument processing** (`qlib/data/inst_processor.py:6 InstProcessor`).

### 1.8 Instrument filtering — `qlib/data/filter.py`

`BaseDFilter:15` (`from_config:29`, `to_config:40`), `SeriesDFilter:51`,
`NameDFilter:265` (regex on instrument names), `ExpressionDFilter:312`
(expression-based universe rule). Filters plug into `D.instruments(market, filter_pipe=[...])`.

### 1.9 Coupling to qlib's own data format

**High.** The `.bin`-float32 layout, the calendar `.txt` index semantics, the
`(instrument, datetime)` MultiIndex, and the `$field` naming convention are baked
into `FileFeatureStorage`, `LocalDatasetProvider`, `Cal.locate_index`, and every
operator (`Ref`, `Mean`, …) which resolves `$close` through the provider stack.
A platform must either (a) export to qlib's binary format via
`FileFeatureStorage.write`, or (b) implement the provider ABCs.

---

## 2. Dataset handlers / benchmarks — `qlib/contrib/data/`

### 2.1 Alpha158 / Alpha360

| Class | Location | Features |
|---|---|---|
| `Alpha360` | `qlib/contrib/data/handler.py:48` | **360** |
| `Alpha360vwap` | `qlib/contrib/data/handler.py:93` | 360, vwap label |
| `Alpha158` | `qlib/contrib/data/handler.py:98` | **158** |
| `Alpha158vwap` | `qlib/contrib/data/handler.py:155` | 158, vwap label |

Feature counts were verified by executing the generators
(`Alpha158DL.get_feature_config()` → `len(fields) == 158`;
`Alpha360DL.get_feature_config()` → `len(fields) == 360`).

- `Alpha158` default feature config (`handler.py:140-150`): `kbar` (9 hard-coded
  K-bar features), `price` with `windows=[0]` and `["OPEN","HIGH","LOW","VWAP"]`,
  and `rolling` with the default windows `[5,10,20,30,60]`.
- `Alpha158DL.get_feature_config` (`qlib/contrib/data/loader.py:73`) supports
  `kbar` / `price` / `volume` / `rolling` sections with `windows`, `include`,
  `exclude`; rolling families are `ROC, MA, STD, BETA, RSQR, RESI, MAX, LOW, QTLU,
  QTLD, RANK, RSV, …` (all defined in `loader.py:130-200`).
- `Alpha360DL` (`loader.py:4`) is *raw normalized price history*: 60 days ×
  {CLOSE, OPEN, HIGH, LOW, VWAP, VOLUME}, each divided by the latest `$close`
  (`loader.py:20-58`).
- **Label** (both): `Ref($close, -2)/Ref($close, -1) - 1` —
  `handler.py:89-90` (Alpha360), `:151-152` (Alpha158). This is a *forward*
  2-day-relative 1-day return; note it is **not** the common `Ref($close,-1)/$close-1`.
- Default processor chains: `_DEFAULT_LEARN_PROCESSORS` = `DropnaLabel` +
  `CSZScoreNorm(fields_group="label")` (`handler.py:37-40`);
  `_DEFAULT_INFER_PROCESSORS` = `ProcessInf` + `ZScoreNorm` + `Fillna`
  (`handler.py:41-45`). **`Alpha158` defaults `infer_processors=[]`**
  (`handler.py:103`), so cross-sectional normalisation is opt-in.
- `check_transform_proc:12` auto-injects `fit_start_time`/`fit_end_time` into any
  processor whose `__init__` accepts them (`:19-30`).

### 2.2 High-frequency handlers

| Class | Location | Kind |
|---|---|---|
| `HighFreqHandler` | `qlib/contrib/data/highfreq_handler.py:8` | `DataHandlerLP`, `freq="1min"` |
| `HighFreqGeneralHandler` | `:103` | configurable 1-min |
| `HighFreqBacktestHandler` | `:199` | `DataHandler` (raw OHLCV for backtest) |
| `HighFreqGeneralBacktestHandler` | `:252` | |
| `HighFreqOrderHandler` | `:307` | order-book/level fields |
| `HighFreqBacktestOrderHandler` | `:462` | |

They depend on the custom high-frequency operators
(`DayLast`, `DayCumsum`, `Select`, `FFillNan`, …) and express features such as
`"{0}/DayLast(Ref({1}, 243))"` to normalise against the previous day's 237th
minute (`highfreq_handler.py:45-58`).

Supporting: `HighFreqTrans:10` / `HighFreqNorm:24`
(`qlib/contrib/data/highfreq_processor.py`), `HighFreqProvider:18`
(`highfreq_provider.py`) — an end-to-end "generate HF dataset → backtest" driver.

### 2.3 Other contrib data pieces

| Item | Location | Note |
|---|---|---|
| `ConfigSectionProcessor` | `qlib/contrib/data/processor.py:7` | the *only* contrib processor; delegates to a configurable transform (`_transform:26`) |
| `ArcticFeatureProvider` | `qlib/contrib/data/data.py:19` | MongoDB/**Arctic**-backed `FeatureProvider` — an alternative data source |
| `MTSDatasetH` | `qlib/contrib/data/dataset.py:103` | multi-time-series dataset for TS models; `_to_tensor:18`, `_create_ts_slices:24`, `assign_data:243` |
| `SepDataFrame` / `SDFLoc` | `qlib/contrib/data/utils/sepdf.py:17` / `:148` | "separated DataFrame" helper (`align_index:7`) |
| `Alpha360DL` / `Alpha158DL` | `qlib/contrib/data/loader.py:4` / `:61` | the generators behind the handlers |

### 2.4 Handler YAML configs

**None ship in the wheel** (0 `.yaml` files in the package). The canonical task
configs (`workflow_config_lightgbm_Alpha158.yaml`, etc.) are repo-only
(`examples/benchmarks/`). `qlib/tests/config.py` gives programmatic builders
instead: `get_data_handler_config:53`, `get_dataset_config:69`, `get_gbdt_task:94`.

---

## 3. Workflow — `qlib/workflow/`

### 3.1 Experiment tracking (MLflow-backed)

`qlib/workflow/__init__.py:2-14` states the design rationale explicitly: *"Better
design than mlflow native design … we have record object with a lot of
methods(more intuitive), instead of use run_id everytime in mlflow … Logging code
diff at the start of run. log_object and load_object to for Python object
directly…"*.

| Class | Location | Key methods |
|---|---|---|
| `QlibRecorder` | `qlib/workflow/__init__.py:26` | `start:38` (contextmanager), `start_exp:97`, `end_exp:147`, `search_records:165`, `list_experiments:200`, `list_recorders:214`, `get_exp:242`, `delete_exp:325`, `get_uri:345`, `set_uri:361`, `uri_context:373`, `get_recorder:392`, `delete_recorder:461`, `save_objects:481`, `load_object:536`, `log_params:542`, `log_metrics:567`, `log_artifact:592`, `download_artifact:608`, `set_tags:630` |
| `RecorderWrapper` | `qlib/workflow/__init__.py:656` | guards against re-`init` while an experiment is active (`register:661`) |
| `R` (global) | `qlib/workflow/__init__.py:681` | `R: QlibRecorderWrapper = RecorderWrapper()` |
| `Experiment` / `MLflowExperiment` | `qlib/workflow/exp.py:15` / `:243` | |
| `ExpManager` / `MLflowExpManager` | `qlib/workflow/expm.py:23` / `:318` | |
| `Recorder` / `MLflowRecorder` | `qlib/workflow/recorder.py:28` / `:247` | `save_objects:74`/`:397`, `load_object:90`/`:413` |

Default backend is MLflow with a local `file:` URI:
`qlib/config.py:33-36 MLflowSettings` (`uri = "file:<cwd>/mlruns"`,
`default_exp_name = "Experiment"`), wired at `qlib/config.py:218-225`
(`exp_manager` → `MLflowExpManager`). `MLflowRecorder._log_uncommitted_code:362`
logs the git diff of uncommitted changes — a genuinely useful reproducibility feature.

### 3.2 Task training and the `qrun` entry point

| Item | Location |
|---|---|
| `task_train` | `qlib/model/trainer.py:108` |
| `begin_task_train` / `end_task_train` | `qlib/model/trainer.py:74` / `:91` |
| `_exe_task` | `qlib/model/trainer.py:42` |
| `_log_task_info` | `qlib/model/trainer.py:36` |
| `Trainer` / `TrainerR` / `DelayTrainerR` / `TrainerRM` / `DelayTrainerRM` | `qlib/model/trainer.py:131 / 209 / 293 / 341 / 491` |
| `workflow()` CLI | `qlib/cli/run.py:86` |
| `run()` (fire entry) | `qlib/cli/run.py:152` → `qrun` console script |

`_exe_task` (`trainer.py:42-71`) is the canonical task lifecycle: build model and
dataset via `init_instance_by_config`, `model.fit(dataset, reweighter)`, persist
`params.pkl` and the dataset (with `dump_all=False, recursive=True`), substitute
`<MODEL>`/`<DATASET>` placeholders (`qlib/utils/__init__.py:758 fill_placeholder`),
then instantiate and `generate()` each `record`.

`qlib/cli/run.py:86-150` supports a `BASE_CONFIG_PATH` inheritance mechanism so a
project YAML can override a shared benchmark YAML.

### 3.3 Task generation and management

| Class / function | Location | Role |
|---|---|---|
| `task_generator` | `qlib/workflow/task/gen.py:15` | apply generators to tasks |
| `TaskGen` | `qlib/workflow/task/gen.py:52` | ABC, `generate:71` |
| `handler_mod` | `qlib/workflow/task/gen.py:93` | adjust handler window per rolling task |
| `trunc_segments` | `qlib/workflow/task/gen.py:127` | guard against label leakage |
| `RollingGen` | `qlib/workflow/task/gen.py:141` | `ROLL_EX` expanding / `ROLL_SD` sliding (`:142-143`), `step`, `trunc_days` |
| `MultiHorizonGenBase` | `qlib/workflow/task/gen.py:305` | multi-horizon label variants |
| `TaskManager` | `qlib/workflow/task/manage.py:33` | **MongoDB-backed** task pool: `create_task:215`, `fetch_task:263`, `safe_fetch_task:287`, `commit_task_res:352`, `return_task:369`, `task_stat:396`, `prioritize:432`, `wait:456` |
| `run_task` | `qlib/workflow/task/manage.py:483` | worker loop |
| `Collector` / `MergeCollector` / `RecorderCollector` | `qlib/workflow/task/collect.py:19 / 90 / 136` | harvest artifacts from recorders |
| `get_mongodb` | `qlib/workflow/task/utils.py:22` | pymongo connection |
| `TimeAdjuster` | `qlib/workflow/task/utils.py:82` | `SHIFT_EX`/`SHIFT_SD`, `align_idx:119`, `truncate:203`, `shift:243` |
| `replace_task_handler_with_cache` | `qlib/workflow/task/utils.py:283` | cache the handler across tasks |

MongoDB config: `qlib/config.py:239-242` (`mongodb://localhost:27017/`,
`default_task_db`).

### 3.4 Record templates — `qlib/workflow/record_temp.py`

| Class | Location | Produces |
|---|---|---|
| `RecordTemp` | `:29` | base; `save:50`, `load:82`, `check:121` |
| `SignalRecord` | `:162` | `pred.pkl`, `label.pkl` (`generate:191`, `generate_label:173`) |
| `ACRecordTemp` | `:213` | auto-checking base, `skip_existing` |
| `HFSignalRecord` | `:249` | HF predictions |
| `SigAnaRecord` | `:296` | IC / ICIR / Rank IC / Rank ICIR, optional long-short (`_generate:311`) |
| `PortAnaRecord` | `:359` | full backtest + risk/indicator analysis (`_generate:466`) |
| `MultiPassPortAnaRecord` | `:570` | repeat backtest with shuffled init scores (`pass_num=10`, `:589`) |

`PortAnaRecord`'s default config (`record_temp.py:397-420`) is the de-facto
"reference A-share backtest": `TopkDropoutStrategy(signal="<PRED>", topk=50,
n_drop=5)`, account 1e8, benchmark `SH000300`, `limit_threshold=0.095`,
`deal_price="close"`, `open_cost=0.0005`, `close_cost=0.0015`, `min_cost=5`.

Contrib extras: `qlib/contrib/workflow/record_temp.py:19 MultiSegRecord`,
`:60 SignalMseRecord`.

---

## 4. Model zoo

### 4.1 Core model framework

| Item | Location |
|---|---|
| `BaseModel` | `qlib/model/base.py:10` (`predict:14`, `__call__:17`) |
| `Model` | `qlib/model/base.py:22` (`fit:25`, `predict:63`) |
| `ModelFT` | `qlib/model/base.py:81` (`finetune:85`) |
| `task_train` | `qlib/model/trainer.py:108` |
| `ConcatDataset` / `IndexSampler` | `qlib/model/utils.py:7` / `:18` |
| `FeatureInt` / `LightGBMFInt` | `qlib/model/interpret/base.py:12` / `:27` |

`Model.fit`'s docstring (`qlib/model/base.py:25-58`) defines the contract and warns
that learned attribute names **must not start with `_`** so they pickle.

Models are **not registered in a registry**: they are referenced by
`{"class": ..., "module_path": ...}` and instantiated by
`qlib/utils/mod.py:122 init_instance_by_config`. `qlib/contrib/model/__init__.py`
is empty.

### 4.2 `qlib/model/ens/` — ensembles

| Class | Location | Behaviour |
|---|---|---|
| `Ensemble` | `qlib/model/ens/ensemble.py:14` | ABC |
| `SingleKeyEnsemble` | `:32` | unwrap single-key dicts, recursive |
| `RollingEnsemble` | `:65` | concat rolling frames sorted by datetime, keep latest duplicate |
| `AverageEnsemble` | `:91` | average + standardise same-shape frames |
| `Group` | `qlib/model/ens/group.py:20` | `group:39`, `reduce:53`, `__call__:67` (joblib `n_jobs`) |
| `RollingGroup` | `qlib/model/ens/group.py:92` | regroup rolling keys; default `ens=RollingEnsemble()` |

`AverageEnsemble` is the default signal combiner in `OnlineManager.prepare_signals`
(`qlib/workflow/online/manager.py:258`).

### 4.3 `qlib/contrib/model/` — exhaustive enumeration (35 files)

All predictive models live in **contrib**, not core. Heavy-dependency column is
read from each file's top-level imports.

| File | Main class(es) | Line | Hard dep |
|---|---|---|---|
| `gbdt.py` | `LGBModel` | `:16` | **lightgbm** ✅ |
| `highfreq_gdbt_model.py` | `HFLGBModel` | `:15` | **lightgbm** ✅ |
| `double_ensemble.py` | `DEnsembleModel` | `:15` | **lightgbm** ✅ |
| `linear.py` | `LinearModel` | `:17` | scikit-learn, scipy ✅ |
| `xgboost.py` | `XGBModel` | `:15` | **xgboost** ❌ |
| `catboost_model.py` | `CatBoostModel` | `:17` | **catboost** ❌ |
| `pytorch_nn.py` | `DNNModelPytorch` | `:38` | **torch** ❌ |
| `pytorch_gru.py` | `GRU` | `:25` | **torch** ❌ |
| `pytorch_gru_ts.py` | `GRU` | `:26` | **torch** ❌ *(TS)* |
| `pytorch_lstm.py` | `LSTM` | `:24` | **torch** ❌ |
| `pytorch_lstm_ts.py` | `LSTM` | `:25` | **torch** ❌ *(TS)* |
| `pytorch_alstm.py` | `ALSTM` | `:25` | **torch** ❌ |
| `pytorch_alstm_ts.py` | `ALSTM` | `:28` | **torch** ❌ *(TS)* |
| `pytorch_gats.py` | `GATs` | `:26` | **torch** ❌ |
| `pytorch_gats_ts.py` | `GATs` (+`DailyBatchSampler:26`) | `:44` | **torch** ❌ *(TS)* |
| `pytorch_transformer.py` | `TransformerModel` | `:27` | **torch** ❌ |
| `pytorch_transformer_ts.py` | `TransformerModel` | `:25` | **torch** ❌ *(TS)* |
| `pytorch_localformer.py` | `LocalformerModel` | `:28` | **torch** ❌ |
| `pytorch_localformer_ts.py` | `LocalformerModel` | `:26` | **torch** ❌ *(TS)* |
| `pytorch_tcn.py` | `TCN` | `:26` | **torch** ❌ |
| `pytorch_tcn_ts.py` | `TCN` | `:25` | **torch** ❌ *(TS)* |
| `pytorch_tabnet.py` | `TabnetModel` | `:25` | **torch** ❌ |
| `pytorch_sfm.py` | `SFM` (`SFM_Model:25`) | `:180` | **torch** ❌ |
| `pytorch_sandwich.py` | `Sandwich` (`SandwichModel:25`) | `:97` | **torch** ❌ |
| `pytorch_hist.py` | `HIST` | `:27` | **torch** ❌ |
| `pytorch_igmtf.py` | `IGMTF` | `:27` | **torch** ❌ |
| `pytorch_krnn.py` | `KRNN` | `:225` | **torch** ❌ |
| `pytorch_tcts.py` | `TCTS` | `:24` | **torch** ❌ |
| `pytorch_add.py` | `ADD` | `:29` | **torch** ❌ |
| `pytorch_adarnn.py` | `ADARNN` | `:23` | **torch** ❌ |
| `pytorch_tra.py` | `TRAModel` | `:33` | **torch** ❌ |
| `pytorch_general_nn.py` | `GeneralPTNN` | `:33` | **torch** ❌ |
| `tcn.py` | `Chomp1d:7`, `TemporalBlock:16`, `TemporalConvNet:52` | — | **torch** ❌ (shared by `pytorch_tcn*`) |
| `pytorch_utils.py` | `count_parameters` | `:7` | **torch** ❌ |

**Counts:** 27 `pytorch_*.py` files; **7** are `*_ts.py` end-to-end time-series
variants (`gru_ts`, `lstm_ts`, `alstm_ts`, `gats_ts`, `transformer_ts`,
`localformer_ts`, `tcn_ts`) which consume `TSDatasetH`/`TSDataSampler` and produce
`<batch, feature, timestep>` tensors; the rest consume flat tabular features.
`DEnsembleModel`, `TRAModel`, `TCTS`, `ADD`, `ADARNN` are multi-stage/transfer
methods with their own `_ts`-less designs.

**No TensorFlow models exist** in 0.9.7. (`grep` for `tensorflow` in
`qlib/contrib/model/` returns nothing.) Any claim that qlib ships TF models is
false for this version.

**As installed**, only `LGBModel`, `HFLGBModel`, `DEnsembleModel`, `LinearModel`
are importable. All 27 PyTorch models raise `ModuleNotFoundError: torch`.

---

## 5. Backtest — `qlib/backtest/`

### 5.1 What makes it different from a vectorised backtest

qlib's backtester is an **event-driven, multi-level (nested) execution engine** with
order-level realism. The distinguishing mechanics, with evidence:

| Capability | Evidence |
|---|---|
| **Nested decision execution** (e.g. daily outer strategy → minute inner executor/strategy) | `qlib/backtest/executor.py:310 NestedExecutor`; docstring `:311-313` *"Nested Executor with inner strategy and executor — At each time `execute` is called, it will call the inner strategy and executor to execute the `trade_decision` in a higher frequency env."* |
| Nested infra/account plumbing | `NestedExecutor.reset_common_infra:375` — *"The lower level have to copy the trade_account"* (`:386-387`) |
| Inner-loop reset per outer step | `NestedExecutor._init_sub_trading:389` |
| Outer strategy may revise decision each inner step | `NestedExecutor._update_trade_decision:396` |
| Order-level simulator | `SimulatorExecutor:513` with `TT_SERIAL`/`TT_PARAL` (`:517`, `:520`) |
| **Limit-up/limit-down handling** | `Exchange.check_stock_limit:338`; reads `limit_buy`/`limit_sell` quote fields with `method="all"` (`:363-368`); direction-aware (`:366-371`). `_update_limit:273` computes the flags; `_get_limit_type:262` supports float / tuple-of-expressions / `None` (`LT_FLT` vs `LT_TP_EXP`) |
| **Suspension handling** | `Exchange.check_stock_suspended:378` — *"suspended stocks are represented by None `$close` stock"* (`:380-381`); *"if all returned is nan, then the stock is suspended"* (`:388`) |
| Combined tradability | `Exchange.is_stock_tradable:404` = not suspended and not limited; `check_order:417` |
| **Trade-unit rounding (A-share 100 shares)** | `Exchange.round_amount_by_trade_unit:761`; `trade_unit` from `C.trade_unit` (`:157`), CN default **100** (`qlib/config.py:297`) |
| **Volume/capacity clipping** | `Exchange._clip_amount_by_volume:786`; `volume_threshold` accepts `("cum"\|"current", limit_expr)` per side (`:295-336`); uses custom ops like `DayCumsum($volume,'9:45','14:45')` (`:98-105`) |
| **Cost model** | `open_cost=0.0015`, `close_cost=0.0025`, `min_cost=5.0`, `impact_cost=0.0` defaults (`:47-50`); quadratic impact `impact_cost * (trade_val/total_trade_val)**2` (`_calc_trade_info_by_order:890-894`); `trade_cost = max(trade_val*cost_ratio, min_cost)` (`:945`) |
| Cash-constraint clipping | `:920-940` — buy clipped if cash cannot cover value + cost |
| **T+1 cash settlement** | `BasePosition.settle_start:201` docstring — `"cash": make the cash settlement delayed. The cash you get can't be used in current step`; `Position.settle_start:487`, `settle_commit:493`, `ST_CASH:198`, `ST_NO:199`; applied in `Position.update_order:390` via `cash_delay` (`:377-379`) |
| Region defaults | `qlib/config.py:295-311`: CN `trade_unit=100`, `limit_threshold=0.095`, `deal_price=close`; US `trade_unit=1`, `limit_threshold=None`; TW `trade_unit=1000`, `limit_threshold=0.1` |
| Quote backends (perf) | `qlib/backtest/high_performance_ds.py:23 BaseQuote`, `PandasQuote:103`, `NumpyQuote:128` (default `quote_cls`), `IndexData`-based |
| **Order-level PA / FFR indicators** | `qlib/backtest/report.py:249 Indicator`; price advantage `_agg_order_price_advantage:524`, fulfill rate `_cal_trade_fulfill_rate:554`, `_cal_trade_positive_rate:584`, deal amount `:590`, trade value `:596` |
| Brinson attribution | `qlib/backtest/profit_attribution.py:226 brinson_pa` (+ `decompose_portofolio:110`, `get_stock_group:205`) |

### 5.2 Module inventory

| Module | Key classes / functions |
|---|---|
| `qlib/backtest/exchange.py` | `Exchange:28` — `__init__:38`, `_get_limit_type:262`, `_update_limit:273`, `_get_vol_limit:295`, `check_stock_limit:338`, `check_stock_suspended:378`, `is_stock_tradable:404`, `check_order:417`, `deal_order:421`, `get_quote_info:465`, `get_close:475`, `get_volume:484`, `get_deal_price:494`, `get_factor:516`, `generate_amount_position_from_weight_position:534`, `generate_order_for_target_amount_position:611`, `get_amount_of_trade_unit:728`, `round_amount_by_trade_unit:761`, `_clip_amount_by_volume:786`, `_calc_trade_info_by_order:859`, `get_order_helper:954` |
| `qlib/backtest/decision.py` | `OrderDir:30`, `Order:37` (`amount_delta:90`, `deal_amount_delta:99`, `sign:108`, `key_by_day:139`, `date:149`), `OrderHelper:154` (`create:166`), `TradeRange:206`, `IdxTradeRange:252`, `TradeRangeByTime:264`, `BaseTradeDecision:302` (`get_decision:344`, `update:361`, `get_range_limit:391`, `mod_inner_decision:518`), `EmptyTradeDecision:539`, `TradeDecisionWO:547`, `TradeDecisionWithDetails:581` |
| `qlib/backtest/executor.py` | `BaseExecutor:22` (`execute:183`, `collect_data:227`, `get_all_executors:305`), `NestedExecutor:310`, `_retrieve_orders_from_decision:501`, `SimulatorExecutor:513` |
| `qlib/backtest/position.py` | `BasePosition:16`, `Position:231` (`_buy_stock:342`, `_sell_stock:352`, `settle_start:487`, `settle_commit:493`), `InfPosition:503` (unlimited capital) |
| `qlib/backtest/account.py` | `AccumulatedInfo:35` (`add_return_value:49`, `add_cost:52`, `add_turnover:55`), `Account:71` (`update_portfolio_metrics:250`, `update_indicator:303`, `update_bar_end:338`, `get_portfolio_metrics:405`, `get_trade_indicator:415`) |
| `qlib/backtest/signal.py` | `Signal:16`, `SignalWCache:36`, `ModelSignal:68`, `create_signal_from:88` |
| `qlib/backtest/backtest.py` | `backtest_loop:26`, `collect_data_loop:53` (a **generator** — this is how RL taps into the loop) |
| `qlib/backtest/report.py` | `PortfolioMetrics:22`, `Indicator:249` |
| `qlib/backtest/utils.py` | `TradeCalendarManager:23` (`get_step_time:102`, `get_data_cal_range:133`), `BaseInfrastructure:204`, `CommonInfrastructure:235`, `LevelInfrastructure:240`, `get_start_end_idx:271` |
| `qlib/backtest/__init__.py` | `get_exchange:33`, `create_account_instance:113`, `get_strategy_executor:177`, `backtest:217`, `collect_data:279`, `format_decisions:312`; `__all__ = ["Order","backtest","get_strategy_executor"]:349` |

Known in-source caveats: `SimulatorExecutor:516-518` — *"TODO: TT_SERIAL & TT_PARAL
will be replaced by feature fix_pos now. Please remove them in the future."*;
`ModelSignal._update_model:104` raises `NotImplementedError("_update_model is not
implemented!")` with a TODO that the method *"is not included in the framework and
could be refactor later"*.

---

## 6. Strategy / portfolio

### 6.1 Core strategy base — `qlib/strategy/base.py`

| Class | Location |
|---|---|
| `BaseStrategy` | `qlib/strategy/base.py:23` |
| `RLStrategy` | `qlib/strategy/base.py:240` |
| `RLIntStrategy` | `qlib/strategy/base.py:261` (`generate_trade_decision:292`) |

`BaseStrategy` API: `executor:63`, `trade_calendar:67`, `trade_position:71`,
`trade_exchange:75`, `reset_level_infra:79`, `reset_common_infra:85`, `reset:91`,
`generate_trade_decision:133`, `get_data_cal_avail_range:149`,
`update_trade_decision:184`, `alter_outer_trade_decision:205`,
`post_upper_level_exe_step:223`, `post_exe_step:229`.

> **Correction to the brief:** `TopkDropoutStrategy` is **not** in
> `qlib/strategy/base.py`. `qlib/strategy/__init__.py` is empty (license header
> only). `TopkDropoutStrategy` lives at
> **`qlib/contrib/strategy/signal_strategy.py:75`**.

### 6.2 Contrib strategies — `qlib/contrib/strategy/`

| Class | Location | Notes |
|---|---|---|
| `BaseSignalStrategy` | `signal_strategy.py:25` | `get_risk_degree:66` |
| `TopkDropoutStrategy` | `signal_strategy.py:75` | params `topk`, `n_drop`, `method_sell`, `method_buy`, `hold_thresh=1`, `only_tradable=False`, `forbid_all_trade_at_limit=True` (`:81-88`); decision logic `:138` |
| `WeightStrategyBase` | `signal_strategy.py:298` | target-weight API: `generate_target_weight_position:330`, `generate_trade_decision:345` |
| `EnhancedIndexingStrategy` | `signal_strategy.py:375` | `get_risk_data:436`, `generate_target_weight_position:462` |
| `TWAPStrategy` | `rule_strategy.py:22` | intraday TWAP execution |
| `SBBStrategyBase` | `rule_strategy.py:125` | sell-below-buy price-trend execution |
| `SBBStrategyEMA` | `rule_strategy.py:297` | SBB with EMA trend predictor |
| `ACStrategy` | `rule_strategy.py:383` | Almgren-Chriss style |
| `RandomOrderStrategy` | `rule_strategy.py:539` | |
| `FileOrderStrategy` | `rule_strategy.py:596` | replay an order list |
| `SoftTopkStrategy` | `cost_control.py:13` | soft top-k with turnover/cost control: `get_risk_degree:47`, `generate_target_weight_position:55` |
| `OrderGenerator` | `order_generator.py:14` | |
| `OrderGenWInteract` | `order_generator.py:50` | order generation **with** sell→buy interaction |
| `OrderGenWOInteract` | `order_generator.py:142` | without interaction |

`qlib/contrib/strategy/__init__.py` exports: `TopkDropoutStrategy`,
`WeightStrategyBase`, `EnhancedIndexingStrategy`, `TWAPStrategy`,
`SBBStrategyBase`, `SBBStrategyEMA`, `SoftTopkStrategy`.

`TopkDropoutStrategy` carries four open TODOs (`signal_strategy.py:76-79`),
including *"Regenerate results with forbid_all_trade_at_limit set to false and flip
the default to false, as it is consistent with reality."* — i.e. the default
trading assumption is acknowledged to be unrealistic.

### 6.3 Optimisers — `qlib/contrib/strategy/optimizer/`

| Class | Location | Deps |
|---|---|---|
| `BaseOptimizer` | `optimizer/base.py:7` | — |
| `PortfolioOptimizer` | `optimizer/optimizer.py:14` | numpy, pandas, **scipy.optimize** ✅ |
| `EnhancedIndexingOptimizer` | `optimizer/enhanced_indexing.py:16` | numpy, **cvxpy** ✅ |

`PortfolioOptimizer` supports `gmv` / `mvo` / `rp` / `inv`
(`optimizer.py:19-25`), params `method`, `lamb` (risk aversion), `delta` (turnover
limit), `alpha` (L2 regulariser), `scale_return`, `tol` (`:33-41`). Docstring:
*"This optimizer always assumes full investment and no-shorting."*
Internal solvers: `_optimize_inv:139`, `_optimize_gmv:146`, `_optimize_mvo:156`,
`_optimize_rp:169`, objectives `_get_objective_*:179-218`, constraints
`_get_constrains:220`, `_solve:241`.

`EnhancedIndexingOptimizer` is a genuine factor-risk-constrained optimiser. Its
docstring states the exact program (`enhanced_indexing.py:25-39`):

```
max_w  d @ r - lamb * (v @ cov_b @ v + var_u @ d**2)
s.t.   w >= 0 ; sum(w) == 1 ; sum(|w - w0|) <= delta
       d >= -b_dev ; d <= b_dev ; v >= -f_dev ; v <= f_dev      where d = w - wb, v = d @ F
```

Params `lamb`, `delta=0.2`, `b_dev=0.01`, `f_dev`, `scale_return`, `epsilon=5e-5`,
`solver_kwargs` (`:47-59`). It consumes `(r, F, cov_b, var_u, w0, wb)` — i.e. it
**requires a factor risk model** as input, which is exactly what
`qlib/model/riskmodel/` produces.

### 6.4 Risk models — `qlib/model/riskmodel/`

| Class | Location | Method |
|---|---|---|
| `RiskModel` | `riskmodel/base.py:12` | `predict:40`, `_predict:113`, `_preprocess:133`; params `nan_option`, `assume_centered`, `scale_return` (`:22`) |
| `ShrinkCovEstimator` | `riskmodel/shrink.py:7` | shrinkage `S_hat = (1-alpha)S + alpha*F` (`:9-10`); `alpha ∈ {lw, oas, float}`, `target ∈ {const_var, const_corr, single_factor, ndarray}` (`:11-24`); Ledoit-Wolf/OAS parameter estimators `_get_shrink_param_lw_*:188,205,231`; OAS `:167` |
| `POETCovEstimator` | `riskmodel/poet.py:6` | POET factor model; `num_factors`, `thresh`, `thresh_method ∈ {soft, hard}` (`:19`) |
| `StructuredCovEstimator` | `riskmodel/structured.py:11` | `X = B F.T + U`; `cov = F cov(B.T) F.T + diag(var(U))` (`:14-16`); `factor_model ∈ {pca, fa}`, `num_factors=10` (`:45`); uses sklearn `PCA` / `FactorAnalysis` (`:66`) |

These are **core** (`qlib/model/`, not contrib), sklearn/numpy-backed, and
self-contained. They are the natural input to `EnhancedIndexingOptimizer`.

---

## 7. Serving / online

### 7.1 `qlib/workflow/online/` — the current online framework

The module docstring (`qlib/workflow/online/manager.py:6-12`) defines the model:

> *"OnlineManager can manage a set of `Online Strategy` and run them dynamically.
> With the change of time, the decisive models will be also changed. In this module,
> we call those contributing models `online` models. In every routine(such as every
> day or every minute), the `online` models may be changed and the prediction of
> them needs to be updated."*

It documents a 2×2 matrix of `{Online, Simulation} × {Trainer, DelayTrainer}`
(`manager.py:20-38`), with pseudo-code at `:40-90`.

| Class | Location | Key methods |
|---|---|---|
| `OnlineManager` | `online/manager.py:101` | `first_train:156`, `routine:184`, `get_collector:230`, `add_strategy:246`, `prepare_signals:258`, `get_signals:289`, `simulate:302`, `delay_prepare:349` |
| `OnlineStrategy` | `online/strategy.py:19` | `prepare_tasks:37`, `prepare_online_models:46`, `first_tasks:72`, `get_collector:78` |
| `RollingStrategy` | `online/strategy.py:92` | rolling retrain; `get_collector:123`, `first_tasks:155`, `prepare_tasks:167`, `_list_latest:191` |
| `RMDLoader` | `online/update.py:21` | load model+dataset from a recorder (`get_dataset:29`, `get_model:62`) |
| `RecordUpdater` | `online/update.py:66` | ABC |
| `DSBasedUpdater` | `online/update.py:82` | dataset-based update (`prepare_data:180`, `update:211`, `get_update_data:251`) |
| `PredUpdater` | `online/update.py:270` | extend prediction series |
| `LabelUpdater` | `online/update.py:284` | extend label series |
| `OnlineTool` / `OnlineToolR` | `online/utils.py:19` / `:87` | tag recorders as "online"; `online_models:67/146`, `update_online_pred:76/159` |

**In-source caveat:** `manager.py:81` — *"FIXME: Currently the delay_prepare is not
implemented in a proper way."* and `manager.py:83` — *"# Can we simplify current
workflow?"*.

**What this is:** a *batch* retraining/re-signalling orchestrator layered on the
recorder + `Trainer` + `RollingGen` stack. **What it is not:** a real-time serving
system. There is no broker gateway, no order-router, no websocket/streaming
ingestion, and no low-latency model server anywhere in qlib.

### 7.2 `qlib/contrib/rolling/` — offline rolling

`qlib/contrib/rolling/base.py:24 Rolling`. Its docstring is explicit about scope
(`:25-37`):

> *"The motivation of Rolling Module — It only focus **offlinely** turn a specific
> task to rollinng … Related modules and difference from me: MetaController: It is
> learning how to handle a task … OnlineStrategy: It is focusing on serving a
> model, the model can be updated time dependently in time. Rolling is much simpler
> and is only for testing rolling models offline."*

Entry point (`:38-42`): `python -m qlib.contrib.rolling.base --conf_path <yaml> run`.
Params: `horizon=20`, `step=20`, `h_path`, `train_start`, `test_end`,
`task_ext_conf`, `rolling_exp` (`:46-53`). `DDGDA` at `qlib/contrib/rolling/ddgda.py:69`
extends it for DDG-DA meta-learning. `qlib/contrib/rolling/__main__.py` uses
`find_all_classes("qlib.contrib.rolling", Rolling)` + `fire.Fire` to expose
sub-commands.

Caveat in-source: `base.py:113-116` — *"FIXME: the qlib_init section will be ignored
by me. So we have to design a priority mechanism to solve this issue."*

### 7.3 `qlib/contrib/online/` — **dead / broken legacy code**

This is the strongest single maturity finding in the tree:

- `qlib/contrib/online/operator.py:17` — `from ..backtest.backtest import update_account`
  → resolves to `qlib.contrib.backtest`, **which does not exist**.
- `qlib/contrib/online/operator.py:20-22` — `from .executor import ...` (three
  imports) but **`qlib/contrib/online/executor.py` is not present** in the package.

Verified by executing `import qlib.contrib.online.operator`:

```
ModuleNotFoundError: No module named 'qlib.contrib.backtest'
```

`operator.py` also carries `# pylint: skip-file` / `# flake8: noqa` (`:4-5`), as do
`manager.py:4-5` and `online_model.py:4-5`. **Conclusion: `qlib/contrib/online/`
is unmaintained dead code in 0.9.7 and must not be integrated.** The live
equivalent is `qlib/workflow/online/`.

Files present: `manager.py:17 UserManager`, `online_model.py:13 ScoreFileModel`,
`operator.py:27 Operator`, `user.py:14 User`, `utils.py:20 load_instance`,
`:37 save_instance`, `:51 create_user_folder`, `:60 prepare`.

---

## 8. Meta-learning — `qlib/model/meta/` + `qlib/contrib/meta/`

### 8.1 Core abstractions (core, `qlib/model/meta/`)

| Class | Location | Role |
|---|---|---|
| `MetaTask` | `model/meta/task.py:8` | wraps a normal task + `meta_info`; `get_dataset:46`, `get_meta_input:49` |
| `MetaTaskDataset` | `model/meta/dataset.py:10` | produces lists of `MetaTask`; `prepare_tasks:39`, `_prepare_seg:69` (abstract) |
| `MetaModel` | `model/meta/model.py:10` | `fit:20`, `inference:26` |
| `MetaTaskModel` | `model/meta/model.py:37` | `fit(meta_dataset)`: `:42`, `inference`: `:49` |
| `MetaGuideModel` | `model/meta/model.py:64` | `fit:70`, `inference:74` |

`MetaTaskDataset`'s docstring (`dataset.py:11-25`) states the transfer intent:
*"A meta-model trained on meta-dataset A and then applied to meta-dataset B — Some
pattern are shared between meta-dataset A and B."*
`qlib/model/meta/__init__.py` exports only `MetaTask`, `MetaTaskDataset`.

### 8.2 DDG-DA (contrib, torch)

| Class / function | Location |
|---|---|
| `InternalData` | `contrib/meta/data_selection/dataset.py:23` |
| `MetaTaskDS` | `contrib/meta/data_selection/dataset.py:121` |
| `MetaDatasetDS` | `contrib/meta/data_selection/dataset.py:237` |
| `TimeReweighter` | `contrib/meta/data_selection/model.py:27` |
| `MetaModelDS` | `contrib/meta/data_selection/model.py:40` |
| `TimeWeightMeta` | `contrib/meta/data_selection/net.py:11` |
| `PredNet` | `contrib/meta/data_selection/net.py:43` |
| `ICLoss` | `contrib/meta/data_selection/utils.py:12` |
| `preds_to_weight_with_clamp` | `contrib/meta/data_selection/utils.py:67` |
| `SingleMetaBase` | `contrib/meta/data_selection/utils.py:99` |

**What it does:** DDG-DA ("Data Selection") learns a *time-varying sample weight*
for training data, so the base model is fit on data that resembles the future
regime. `MetaModelDS.__init__` (`model.py:46-65`) takes `step`, `hist_step_n`,
`clip_method="tanh"`, `clip_weight=2.0`, `criterion="ic_loss"`, `lr=1e-4`,
`max_epoch=100`, `alpha`, `loss_skip_thresh`. It produces a `TimeReweighter`
(`model.py:27-37`) that assigns a scalar weight per time slice and plugs into
`Model.fit(dataset, reweighter)` — i.e. it composes with **any** qlib model, not
just neural ones. `PredNet.forward` deliberately de-means predictions:
`net.py:35` — `preds = preds - torch.mean(preds)  # avoid using future information`.

**Maturity:** research reproduction of a published method; contrib-only; requires
**torch** (❌ not installed) via `model.py:5-7`; no tests in the wheel; no
experimental-warning docstring, but it is clearly a paper artifact rather than a
productised feature. The *core* `MetaTask`/`MetaModel` interfaces are small and
stable-looking; the *contrib* implementation is the fragile part.

---

## 9. Reinforcement learning — `qlib/rl/`

### 9.1 Public abstractions (core `qlib/`)

`qlib/rl/__init__.py` exports `Interpreter`, `StateInterpreter`,
`ActionInterpreter`, `Reward`, `RewardCombination`, `Simulator`.

| Abstraction | Location | Notes |
|---|---|---|
| `Interpreter` | `rl/interpreter.py:19` | ABC |
| `StateInterpreter` | `rl/interpreter.py:35` | sim state → observation |
| `ActionInterpreter` | `rl/interpreter.py:67` | policy action → env action |
| `_gym_space_contains` / `GymSpaceValidationError` | `rl/interpreter.py:101` / `:134` | **gym** space validation |
| `Simulator` | `rl/simulator.py:21` | generic `Simulator[InitialState, State, Act]` |
| `Reward` / `RewardCombination` | `rl/reward.py:16` / `:38` | |
| `AuxiliaryInfoCollector` | `rl/aux_info.py:21` | |
| `InitialStateType` (TypeVar only) | `rl/seed.py` | docstring: *"With single-asset order execution only, the only seed is order."* |

### 9.2 Order execution

| Item | Location |
|---|---|
| `SAOEState` / `SAOEMetrics` | `rl/order_execution/state.py:70` / `:18` |
| `FullHistoryStateInterpreter` / `CurrentStepStateInterpreter` | `rl/order_execution/interpreter.py:68` / `:164` |
| `DummyStateInterpreter` | `rl/order_execution/interpreter.py:56` |
| `CategoricalActionInterpreter` / `TwapRelativeActionInterpreter` | `rl/order_execution/interpreter.py:199` / `:233` |
| `Recurrent` / `Attention` nets | `rl/order_execution/network.py:19` / `:122` |
| `NonLearnablePolicy` / `AllOne` | `rl/order_execution/policy.py:25` / `:46` |
| `PPOActor` / `PPOCritic` / `PPO` | `rl/order_execution/policy.py:69` / `:86` / `:102` (**tianshou** `PPOPolicy`) |
| `DQN` | `rl/order_execution/policy.py:164` (**tianshou** `DQNPolicy`) |
| `PAPenaltyReward` / `PPOReward` | `rl/order_execution/reward.py:17` / `:53` |
| `SingleAssetOrderExecution` | `rl/order_execution/simulator_qlib.py:19` — **qlib-backtest-backed** SAOE simulator |
| `SingleAssetOrderExecutionSimple` | `rl/order_execution/simulator_simple.py:24` |
| `SAOEStateAdapter` | `rl/order_execution/strategy.py:71` |
| `SAOEStrategy` / `ProxySAOEStrategy` / `SAOEIntStrategy` | `rl/order_execution/strategy.py:301` / `:407` / `:445` |
| `SingleOrderStrategy` | `rl/strategy/single_order.py:11` |
| `price_advantage` | `rl/order_execution/utils.py:25` |
| `get_simulator_executor` | `rl/order_execution/utils.py:48` |

### 9.3 Trainer / envs / data

| Item | Location |
|---|---|
| `train()` / `backtest()` API | `rl/trainer/api.py:19` / `:66` |
| `Trainer` | `rl/trainer/trainer.py:30` |
| `TrainingVesselBase` / `TrainingVessel` | `rl/trainer/vessel.py:34` / `:98` |
| `Callback` / `EarlyStopping` / `MetricsWriter` / `Checkpoint` | `rl/trainer/callbacks.py:32` / `:78` / `:185` / `:203` |
| `EnvWrapper` | `rl/utils/env_wrapper.py:51` |
| `FiniteVectorEnv` / `FiniteDummyVectorEnv` / `FiniteSubprocVectorEnv` / `FiniteShmemVectorEnv` / `vectorize_env` | `rl/utils/finite_env.py:89` / `:301` / `:305` / `:309` / `:313` (**tianshou** vector envs) |
| `BaseIntradayBacktestData` / `BaseIntradayProcessedData` / `ProcessedDataProvider` | `rl/data/base.py:10` / `:39` / `:55` |
| `IntradayBacktestData` / `DataframeIntradayBacktestData` / `HandlerIntradayProcessedData` / `HandlerProcessedDataProvider` | `rl/data/native.py:30` / `:86` / `:143` / `:203` |
| `SimpleIntradayBacktestData` / `PickleIntradayProcessedData` / `PickleProcessedDataProvider` | `rl/data/pickle_styled.py:98` / `:161` / `:230` |
| `init_qlib` | `rl/data/integration.py:18` |
| `single_with_simulator` / `single_with_collect_data_loop` / `backtest` | `rl/contrib/backtest.py:127` / `:222` / `:317` |
| `train_and_test` / `main` / `LazyLoadDataset` | `rl/contrib/train_onpolicy.py:100` / `:204` / `:51` |

### 9.4 Coupling and maturity

**Coupling:** high, in both directions. `rl/order_execution/simulator_qlib.py:19-84`
drives qlib's *own* backtest engine — it builds `get_strategy_executor(...)` with
`pos_type="Position"`, asserts the executor is a `NestedExecutor` (`:76`), and
consumes `collect_data_loop(...)` as a generator (`:82-88`). It also needs qlib's
1-minute data (`load_backtest_data`, `HandlerIntradayProcessedData`).

**Deps:** **tianshou** (❌), **gym** (✅ legacy), **torch** (❌).
`grep` confirms `from tianshou...` in `policy.py` and `finite_env.py`.

**Maturity:** in core `qlib/rl/`, but narrowly scoped to **single-asset order
execution** (`rl/seed.py` docstring), with no experimental-warning banner but also
no shipped tests. Not usable as installed. There is no portfolio-level RL, no
multi-asset allocation RL.

---

## 10. Analysis / reporting

### 10.1 `qlib/contrib/report/`

| Function / class | Location | Output |
|---|---|---|
| `_group_return` | `analysis_model/analysis_model_performance.py:21` | grouped return by score bucket |
| `_plot_qq` | `:81` | Q-Q plot |
| `_pred_ic` | `:119` | IC series |
| `_pred_autocorr` | `:223` | signal autocorrelation |
| `_pred_turnover` | `:240` | top-N turnover |
| `ic_figure` | `:271` | plotly IC figure |
| `model_performance_graph` | `:293` | the model-report entry point |
| `cumulative_return_graph` | `analysis_position/cumulative_return.py:182` | cumulative return |
| `risk_analysis_graph` | `analysis_position/risk_analysis.py:162` | risk report (annualised return/vol/Sharpe/MDD) |
| `score_ic_graph` | `analysis_position/score_ic.py:25` | score IC |
| `rank_label_graph` | `analysis_position/rank_label.py:62` | rank-vs-label |
| `parse_position` | `analysis_position/parse_position.py:10` | backtest position → DataFrame |
| `get_position_data` | `analysis_position/parse_position.py:138` | |
| `report_graph` | `analysis_position/report.py:166` | combined report (`_calculate_mdd:25`, `_calculate_report_data:35`) |
| `BaseGraph` | `report/graph.py:17` | plotly wrapper |
| `ScatterGraph` / `BarGraph` / `DistplotGraph` / `HeatmapGraph` / `HistogramGraph` | `report/graph.py:141 / 145 / 149 / 165 / 185` | |
| `SubplotsGraph` | `report/graph.py:202` | |
| `sub_fig_generator` | `report/utils.py:7` | |
| `guess_plotly_rangebreaks` | `report/utils.py:49` | |
| `FeaAnalyser` | `report/data/base.py:15` | feature-analysis ABC |
| `CombFeaAna`, `NumFeaAnalyser`, `ValueCNT`, `FeaDistAna`, `FeaInfAna`, `FeaNanAna`, `FeaNanAnaRatio`, `FeaACAna`, `FeaSkewTurt`, `FeaMeanStd`, `RawFeaAna` | `report/data/ana.py:27 / 58 / 66 / 89 / 96 / 112 / 124 / 137 / 151 / 177 / 205` | feature diagnostics (distribution, inf, NaN ratio, autocorr, skew/kurtosis, mean/std) |

### 10.2 `qlib/contrib/eva/` and `evaluate*.py`

| Function | Location |
|---|---|
| `calc_long_short_prec` | `contrib/eva/alpha.py:14` |
| `calc_long_short_return` | `contrib/eva/alpha.py:71` |
| `pred_autocorr` / `pred_autocorr_all` | `contrib/eva/alpha.py:116` / `:143` |
| **`calc_ic`** | `contrib/eva/alpha.py:160` — Pearson + Spearman per date |
| `calc_all_ic` | `contrib/eva/alpha.py:186` (joblib `n_jobs`) |
| `risk_analysis` | `contrib/evaluate.py:27` — annualised return/vol/Sharpe/MDD/information ratio |
| `indicator_analysis` | `contrib/evaluate.py:97` |
| `backtest_daily` | `contrib/evaluate.py:148` |
| `long_short_backtest` | `contrib/evaluate.py:277` |
| `get_annual_return_from_positions`, `get_sharpe_ratio_from_return_series`, `get_max_drawdown_from_series`, `get_beta`, `get_alpha`, `get_rank_ic`, `get_normal_ic`, … | `contrib/evaluate_portfolio.py:122 / 159 / 178 / 200 / 215 / 229 / 243` |

`SigAnaRecord._generate` (`qlib/workflow/record_temp.py:311-350`) uses `calc_ic`
and `calc_long_short_return` to log `IC`, `ICIR`, `Rank IC`, `Rank ICIR` plus
optional long-short annualised return/Sharpe.

### 10.3 Deps and maturity

**Required and missing:** `plotly` (❌ — `report/graph.py`, all `analysis_position/*`),
`statsmodels` (❌ — `analysis_model_performance.py`), `seaborn` (❌ — `report/data/ana.py`).
**Present:** `matplotlib` (✅, used by `analysis_model_performance.py` and
`report/utils.py`), `scipy` (✅).

**Maturity:** entirely `contrib/`; the plotting stack cannot even be imported in
this environment. The *computational* kernels (`calc_ic`, `risk_analysis`) are
plain pandas/numpy and are portable.

---

## 11. Ops / tuner / data utils

### 11.1 `qlib/contrib/ops/high_freq.py` — custom operators

| Operator | Line | Purpose |
|---|---|---|
| `get_calendar_day` | `:13` | helper |
| `get_calendar_minute` | `:39` | helper |
| `DayCumsum` | `:50` | intraday cumulative sum since a time |
| `DayLast` | `:102` | last value of previous day |
| `FFillNan` | `:122` | forward fill NaN |
| `BFillNan` | `:141` | backward fill NaN |
| `Date` | `:160` | date component |
| `Select` | `:180` | conditional select (PairOperator) |
| `IsNull` | `:203` | |
| `IsInf` | `:222` | |
| `Cut` | `:241` | |

These are **not registered by default** — they must be passed via
`qlib.init(custom_ops=[...])` (`qlib/config.py:284`) which
`register_all_ops` consumes (`qlib/data/ops.py:1678-1681`). `Exchange`'s
`volume_threshold` docstring explicitly points at this file
(`qlib/backtest/exchange.py:98-100`).

### 11.2 `qlib/contrib/tuner/` — hyper-parameter tuning

| Class / function | Location |
|---|---|
| `TunerConfigManager` | `tuner/config.py:12` |
| `PipelineExperimentConfig` | `tuner/config.py:33` |
| `OptimizationConfig` | `tuner/config.py:59` |
| `run()` (CLI) | `tuner/launcher.py:31` |
| `Pipeline` | `tuner/pipeline.py:17` (`run:36`, `init_tuner:51`, `save_tuner_exp_info:76`) |
| hyperopt search space | `tuner/space.py:7` (`from hyperopt import hp`) |
| `Tuner` / `QLibTuner` | `tuner/tuner.py:25` / `:84` (`fmin`, `tpe`, `STATUS_OK`/`STATUS_FAIL`) |

Entry point: `python -m qlib.contrib.tuner.launcher -c <config.yaml>`
(`launcher.py:19-29`). `Pipeline.GLOBAL_BEST_PARAMS_NAME = "global_best_params.json"`
(`pipeline.py:18`). **Dep: `hyperopt` ❌ not installed** → unusable as installed.
`tuner.py` shells out via `subprocess` (`:14`) and pickles trial state (`:11`).

### 11.3 `qlib/contrib/data/utils/`

`sepdf.py`: `align_index:7`, `SepDataFrame:17` (dict-of-DataFrames with a joined
index; `loc:55`, `index:59`, `apply_each:62`, `merge:140`), `SDFLoc:148`,
`_isinstance:193`. `__init__.py` is empty.

### 11.4 Contrib processors

Only `ConfigSectionProcessor` (`qlib/contrib/data/processor.py:7`) —
`_transform:26` applies a configurable per-section transform. Notably,
**`CSRankNorm` is core, not contrib** (`qlib/data/dataset/processor.py:326`),
contrary to the framing in the brief.

---

## 12. Utilities

### 12.1 `qlib/utils/__init__.py` (961 lines)

`get_redis_connection:43`, `read_bin:54` (**reads the `.bin` format**),
`get_period_list:71`, `get_period_offset:101`, `read_period_data:109` (PIT record
reader), `np_ffill:176`, `lower_bound:193`, `upper_bound:209`,
`requests_with_retry:226`, `parse_config:242`, `drop_nan_by_y_index:259`,
`hash_args:271`, `parse_field:277`, `compare_dict_value:305`,
`remove_repeat_field:328`, `remove_fields_space:339`, `normalize_cache_fields:350`,
`normalize_cache_instruments:359`, `is_tradable_date:375`, `get_date_range:386`,
`get_date_by_shift:407`, `get_next_trading_date:451`, `get_pre_trading_date:460`,
`transform_end_date:469`, `get_date_in_file_name:493`, `split_pred:506`,
`time_to_slc_point:544`, `can_use_cache:566`, `exists_qlib_data:578`,
`check_qlib_data:638`, `lazy_sort_index:650`, `flatten_dict:681`,
`get_item_from_obj:712`, `fill_placeholder:758`, `auto_filter_kwargs:821`,
`Wrapper:858` (the provider-wrapper pattern), `register_wrapper:876`,
`load_dataset:889`, `code_to_fname:905`, `fname_to_code:925`.

### 12.2 Other utility modules

| Module | Contents |
|---|---|
| `utils/serial.py` | `Serializable:11` — `config:81`, `to_pickle:115`, `load:136`, `get_backend:157`, `general_dump:173`; `__getstate__:47` excludes `_`-prefixed and configured attrs |
| `utils/mod.py` | `get_module_by_module_path:25`, `split_module_path:49`, `get_callable_kwargs:67`, **`init_instance_by_config:122`**, `class_casting:189`, **`find_all_classes:207`** |
| `utils/time.py` | `get_min_cal:32`, `is_single_value:73`, **`Freq:114`** (`parse:141`, `get_timedelta:187`, `get_min_delta:204`, `get_recent_freq:229`), `time_to_day_index:258`, `get_day_min_idx_range:283`, `concat_date_time:309`, `cal_sam_minute:323`, `epsilon_change:349` |
| `utils/file.py` | `get_or_create_path:16`, `save_multiple_parts_file:44`, `unpack_archive_with_buffer:96`, `get_io_object:164` |
| `utils/paral.py` | `ParallelExt:20`, `datetime_groupby_apply:33`, `AsyncCaller:72`, `DelayedTask:141`, `DelayedTuple:163`, `DelayedDict:175`, `complex_parallel:269`, `call_in_subproc:298` (joblib) |
| `utils/index_data.py` | `concat:21`, `sum_by_index:57`, `Index:87`, `LocIndexer:206`, `BinaryOps:316`, `IndexData:346`, `SingleData:529`, `MultiData:621` — the numpy-backed structure behind `NumpyQuote` |
| `utils/resam.py`, `utils/objm.py`, `utils/data.py`, `utils/exceptions.py` | resampling, object management, `update_config`, exception types |
| `log.py` | `MetaLogger:15`, `QlibLogger:24`, `_QLibLoggerManager:51`, **`TimeInspector:86`**, `set_log_with_config:152`, `LogFilter:161`, `set_global_logger_level:185`, `set_global_logger_level_cm:227` |

### 12.3 `qlib/config.py`

| Item | Location |
|---|---|
| `MLflowSettings` (pydantic-settings) | `:33` |
| `QSettings` (`env_prefix="QLIB_"`) | `:38`; `provider_uri` default `~/.qlib/qlib_data/cn_data` (`:52`) |
| `Config` | `:63` (dict-like; `__getattr__:71`, `register_from_C:111`) |
| `_default_config` | `:134` — providers, cache, kernels, redis, logging, `exp_manager`, `pit_record_type:226`, `pit_record_nan:232`, `mongo:239`, `min_data_shift:246` |
| `MODE_CONF` | `:249` — `server` (disk caches + redis) vs `client` (caches off by default, `custom_ops:284`) |
| `HIGH_FREQ_CONFIG` | `:288` |
| `_default_region_config` | `:295` — CN/US/TW trade_unit, limit_threshold, deal_price |
| `QlibConfig` | `:314` — `DataPathManager:324`, `set_mode:386`, `set_region:391`, `dpm:399`, `resolve_path:403`, `set:423`, `register:482`, `reset_qlib_version:503`, `get_kernels:514`, `registered:520` |
| **`C` global** | `:526` — `C = QlibConfig(_default_config)` |

`C.register()` (`:482-501`) is the single wiring point: `register_all_ops(self)` →
`register_all_wrappers(self)` → build `QlibRecorder` from `exp_manager` → `R.register`.

In-source bug note: `config.py:511` — *"Due to a bug? that converting `__version__`
to `_QlibConfig__version_bak`"*.

### 12.4 `qlib/constant.py`

`REG_CN="cn"`, `REG_US="us"`, `REG_TW="tw"`, `EPS=1e-12`, `INF=int(1e18)`,
`ONE_DAY`, `ONE_MIN`, `EPS_T=1s`, `float_or_ndarray` TypeVar. No classes.

### 12.5 CLI

`qlib/cli/run.py:152 run()` → `fire.Fire(workflow)` (the `qrun` entry point).
`qlib/cli/data.py` is a thin `fire.Fire(GetData)` wrapper over
`qlib/tests/data.py:18 GetData` for downloading sample data. `qlib/cli/__init__.py`
is empty.

---

## 13. Extension points (how a user plugs in)

| Extension | Base class / hook | Registration path | Evidence |
|---|---|---|---|
| **Custom model** | subclass `qlib.model.base.Model` (or `ModelFT`) | reference as `{"class": "MyModel", "module_path": "my.pkg"}` in the task dict; resolved by `init_instance_by_config` | `qlib/model/base.py:22`; `qlib/utils/mod.py:122`; consumed at `qlib/model/trainer.py:47-49` |
| **Custom dataset handler** | subclass `DataHandler` or `DataHandlerLP` | same `class`/`module_path` dict inside `dataset.handler` | `qlib/data/dataset/handler.py:68`, `:383`; `qlib/data/dataset/__init__.py:72` |
| **Custom dataset (segmentation)** | subclass `Dataset` / `DatasetH` | `dataset.class` in the task | `qlib/data/dataset/__init__.py:15`, `:72` |
| **Custom processor** | subclass `Processor`; implement `__call__`, optionally `fit`, `readonly`, `is_for_infer` | list entry in `infer_processors` / `learn_processors` / `shared_processors` | `qlib/data/dataset/processor.py:35`, `:49`, `:62`, `:74`; applied at `qlib/data/dataset/handler.py:531 _run_proc_l` |
| **Custom strategy** | subclass `BaseStrategy`; implement `generate_trade_decision` | `strategy` config in backtest / `PortAnaRecord` config | `qlib/strategy/base.py:23`, `:133`; `qlib/backtest/__init__.py:177 get_strategy_executor` |
| **Custom operator / expression** | subclass `ExpressionOps` (or `ElemOperator` / `PairOperator` / `Rolling`) | `qlib.init(custom_ops=[MyOp])` or `[{"class":..., "module_path":...}]`; also `Operators.register([...])` directly | `qlib/data/ops.py:1619 OpsWrapper`, `:1628 register`, `:1670 register_all_ops`; config key `qlib/config.py:284` |
| **Custom data source (provider)** | implement `CalendarProvider` / `InstrumentProvider` / `FeatureProvider` / `ExpressionProvider` / `DatasetProvider` / `PITProvider` | `qlib.init(calendar_provider=..., instrument_provider=..., feature_provider=..., expression_provider=..., dataset_provider=..., provider=...)` | ABCs `qlib/data/data.py:65 / 199 / 307 / 383 / 446 / 338`; keys `qlib/config.py:136-142`; wiring `qlib/data/data.py:1292 register_all_wrappers` |
| **Custom storage backend** | subclass `CalendarStorage` / `InstrumentStorage` / `FeatureStorage` (or the `User*Storage` hooks) | replace the storage class used by a provider | `qlib/data/storage/storage.py:84 / 191 / 255`; `UserCalendarStorage:24`, `UserInstrumentStorage:38`, `UserFeatureStorage:52` |
| **Custom instrument filter** | subclass `BaseDFilter`; implement `_getFilterSeries` | `D.instruments(market, filter_pipe=[...])` | `qlib/data/filter.py:15`, `:51`, `:265`, `:312` |
| **Custom instrument processor** | subclass `InstProcessor`; implement `__call__(df, instrument, ...)` | `inst_processors=` on `D.features` / handler | `qlib/data/inst_processor.py:6`; `qlib/data/data.py:453` |
| **Custom sample reweighter** | subclass `Reweighter`; implement `reweight(data)` | `task["reweighter"]`, passed to `model.fit` | `qlib/data/dataset/weight.py:5`; `qlib/model/trainer.py:50`; `qlib/model/base.py:25` |
| **Custom cache layer** | subclass `ExpressionCache` / `DatasetCache` | `qlib.init(expression_cache=..., dataset_cache=...)` | `qlib/data/cache.py:329`, `:380`; config keys `qlib/config.py:155-156` |
| **Custom experiment backend** | subclass `ExpManager` + `Recorder` (+ `Experiment`) | `qlib.init(exp_manager={...})` | `qlib/workflow/expm.py:23`, `qlib/workflow/recorder.py:28`, `qlib/workflow/exp.py:15` |
| **Custom task generator** | subclass `TaskGen`; implement `generate(task)` | `task_generator(tasks, [MyGen()])` | `qlib/workflow/task/gen.py:52`, `:15` |
| **Custom record template** | subclass `RecordTemp`; implement `generate` | `task["record"]` list | `qlib/workflow/record_temp.py:29`, `:69` |
| **Custom ensemble** | subclass `Ensemble`; implement `__call__` | `qlib/model/ens/`; `OnlineManager.prepare_signals(prepare_func=...)` | `qlib/model/ens/ensemble.py:14`; `qlib/workflow/online/manager.py:258` |
| **Custom RL simulator/interpreter** | subclass `Simulator` / `StateInterpreter` / `ActionInterpreter` / `Reward` | `rl.trainer.api.train(...)` vessel config | `qlib/rl/simulator.py:21`, `qlib/rl/interpreter.py:35`, `:67`, `qlib/rl/reward.py:16` |
| **Config discovery** | `find_all_classes(module_path, cls)` | used by `qlib/contrib/rolling/__main__.py` to auto-expose sub-commands | `qlib/utils/mod.py:207` |

The **single most important extension mechanism** is
`init_instance_by_config` (`qlib/utils/mod.py:122`): every model, dataset,
handler, processor, strategy, executor, record and experiment manager in qlib is
constructed from a `{"class", "module_path", "kwargs"}` dict. A platform can
therefore register its own components without forking qlib, purely through task
YAML.

---

## 14. Maturity summary

| Area | Core vs contrib | Deps present? | Tests in wheel | Notes |
|---|---|---|---|---|
| Data providers / expressions / ops | **core** | ✅ | base classes only | production-grade |
| Cython rolling/expanding | **core** | ✅ (compiled) | — | graceful degradation on ABI mismatch |
| Caching (mem / disk / redis) | **core** | ✅ | — | disk cache silently disabled without Redis |
| Storage (file) | **core** | ✅ | mock storage classes | format is proprietary `.bin` float32 |
| Dataset / handler / processors | **core** | ✅ | — | stable |
| PIT | **core** | ✅ | — | self-declared not thread-safe; no file backend TODO |
| Alpha158 / Alpha360 | contrib | ✅ | — | de-facto standard; label is `Ref(close,-2)/Ref(close,-1)-1` |
| High-frequency handlers | contrib | ✅ | — | needs custom HF ops + 1-min data |
| Workflow / recorder / trainer | **core** | ✅ (mlflow) | — | MLflow hard dep |
| Task manager (MongoDB) | **core** | needs pymongo | — | distributed task pool |
| `qrun` CLI | **core** | ✅ (fire) | — | |
| Model framework + ens + interpret | **core** | ✅ | — | |
| Predictive models | **contrib only** | ❌ mostly | — | 27 torch models unusable; lightgbm/linear usable |
| Risk models | **core** | ✅ | — | shrinkage/POET/structured — genuinely good |
| Backtest / nested executor | **core** | ✅ | — | the strongest differentiator |
| Strategies | contrib | ✅ | — | `TopkDropoutStrategy` here, not core |
| Optimisers | contrib | ✅ (scipy, cvxpy) | — | enhanced indexing needs a risk model |
| Online workflow | **core** | ✅ | — | batch retraining, `delay_prepare` FIXME |
| `contrib/rolling` | contrib | ✅ | — | explicitly offline-only |
| `contrib/online` | contrib | ❌ **broken imports** | — | **dead code — do not integrate** |
| Meta-learning (DDG-DA) | core iface + contrib impl | ❌ torch | — | research artifact |
| RL | **core** | ❌ torch + tianshou | — | single-asset order execution only |
| Reporting | contrib | ❌ plotly/statsmodels/seaborn | — | computational kernels portable |
| Tuner | contrib | ❌ hyperopt | — | |
| HF ops | contrib | ✅ | — | must be registered via `custom_ops` |
| Utils / config / constants | **core** | ✅ | — | `Serializable`, `Freq`, `IndexData` |

### Notable in-source caveats (quoted)

- `qlib/workflow/online/manager.py:81` — *"FIXME: Currently the delay_prepare is not implemented in a proper way."*
- `qlib/contrib/rolling/base.py:25-26` — *"It only focus **offlinely** turn a specific task to rollinng"*
- `qlib/backtest/executor.py:516-518` — *"TODO: TT_SERIAL & TT_PARAL will be replaced by feature fix_pos now. Please remove them in the future."*
- `qlib/backtest/signal.py:104` — *"raise NotImplementedError("_update_model is not implemented!")"*
- `qlib/data/data.py:745` — *"NOTE: This class is not multi-threading-safe!!!!"*
- `qlib/contrib/strategy/signal_strategy.py:78-79` — *"Regenerate results with forbid_all_trade_at_limit set to false and flip the default to false, as it is consistent with reality."*
- `qlib/contrib/rolling/base.py:113-114` — *"FIXME: the qlib_init section will be ignored by me."*
- `qlib/config.py:511` — *"Due to a bug? that converting __version__ to _QlibConfig__version_bak"*
- `qlib/data/dataset/__init__.py:722` — `__all__ = ["Optional", "Dataset", "DatasetH"]` (stray `Optional`)
- `qlib/contrib/online/operator.py:17,20-22` — imports from non-existent `qlib.contrib.backtest` and missing `.executor`

### Corrections to the task brief

1. `TopkDropoutStrategy` is **not** in `qlib/strategy/base.py`; it is
   `qlib/contrib/strategy/signal_strategy.py:75`.
2. `CSRankNorm` is **core** (`qlib/data/dataset/processor.py:326`), not contrib.
3. There are **no TensorFlow models** in 0.9.7.
4. `qlib/contrib/online/` is **broken** and must not be integrated; the live
   online framework is `qlib/workflow/online/`.
5. `qlib/data/dataset/` does **not** define `DatasetProvider`; that lives in
   `qlib/data/data.py:446`.
6. No handler/task YAML ships in the wheel — those are repo-only artifacts.

---

## 15. Integration-readiness ranking for an A-share platform

| Capability | Reuse as-is | Wrap | Reimplement | Why |
|---|---|---|---|---|
| Backtest engine (nested executor, limit/suspend/trade-unit/T+1) | ✅ | | | hardest thing to rebuild; CN defaults already correct (`config.py:295-311`) |
| Expression engine + ops | ✅ | | | 40+ operators, Cython-accelerated, extensible |
| Data format + storage + cache | | ✅ | | adopt the `.bin`/calendar layout, or implement the provider ABCs |
| Dataset/handler/processor pipeline | ✅ | | | leak-safe learn/infer split is well designed |
| Risk models (shrink/POET/structured) | ✅ | | | core, sklearn-only, high quality |
| Enhanced-indexing optimiser | ✅ | | | cvxpy installed; needs a risk model |
| Alpha158/Alpha360 | ✅ | | | 158/360 features, verified; replace the label expression for A-share conventions |
| Workflow + MLflow recorder | ✅ | | | reproducibility incl. git-diff logging |
| `TopkDropoutStrategy` | | ✅ | | realistic defaults acknowledged unrealistic |
| LightGBM / linear models | ✅ | | | installed and working |
| PyTorch model zoo | | | ✅ (or install torch) | 27 models, none importable today |
| Online/rolling retraining | | ✅ | | batch-oriented; `delay_prepare` unfinished |
| Reporting | | ✅ | | computational kernels portable, plotting deps missing |
| Meta-learning (DDG-DA) | | | ✅ | research artifact, torch |
| RL | | | ✅ | single-asset only, torch+tianshou |
| Tuner | | | ✅ | hyperopt missing |
| `contrib/online` | ❌ | | | broken imports — dead code |

### The five capabilities a platform like lquant most likely lacks

1. **Nested intraday decision execution with A-share market microstructure** —
   `NestedExecutor` (daily strategy → minute executor/strategy) combined with
   limit-up/down flags (`Exchange.check_stock_limit:338`), suspension via
   `None $close` (`check_stock_suspended:378`), 100-share trade-unit rounding
   (`round_amount_by_trade_unit:761`), volume/capacity clipping
   (`_clip_amount_by_volume:786`), quadratic impact cost, and **T+1 cash
   settlement** (`Position.settle_start:487`). Very few in-house vectorised
   backtesters model any of this.

2. **A composable expression/operator engine with PIT support and a multi-tier
   cache** — 40+ operators (`qlib/data/ops.py`), algebraic composition via Python
   operator overloading (`qlib/data/base.py:32-137`), `P`/`PRef` point-in-time
   operators (`qlib/data/pit.py:23,62`), plus memory → disk(HDF5) → Redis-locked
   caching (`qlib/data/cache.py:136,489,646`). The PIT path is what makes
   financial-statement factors safe from look-ahead.

3. **Recorder-backed rolling/online retraining orchestration** — `RollingGen`
   (`qlib/workflow/task/gen.py:141`) + `TrainerR`/`TrainerRM`
   (`qlib/model/trainer.py:209,341`) + `OnlineManager`/`RollingStrategy`
   (`qlib/workflow/online/manager.py:101`, `strategy.py:92`) + MLflow artifacts,
   with `trunc_segments:127` leakage guards and `MultiPassPortAnaRecord:570` for
   robustness testing. This is a full experiment-provenance stack, not just a
   training script.

4. **Shrinkage / factor-structured covariance risk models + a factor-constrained
   portfolio optimiser** — `ShrinkCovEstimator` (Ledoit-Wolf, OAS, three targets;
   `qlib/model/riskmodel/shrink.py:7`), `POETCovEstimator:6`,
   `StructuredCovEstimator` (PCA/FA; `structured.py:11`), feeding
   `EnhancedIndexingOptimizer`'s benchmark-deviation/turnover/factor-deviation
   constrained cvxpy program (`contrib/strategy/optimizer/enhanced_indexing.py:25-39`).

5. **Meta-learning-based training-sample reweighting (DDG-DA)** — `MetaTaskDataset`
   / `MetaTaskModel` (`qlib/model/meta/dataset.py:10`, `model.py:37`) with a
   `TimeReweighter` (`contrib/meta/data_selection/model.py:27`) that composes with
   *any* qlib model via `Model.fit(dataset, reweighter)`. Regime-aware sample
   weighting is an unusual capability that most platforms simply do not have.

*Runner-up:* the breadth of the model zoo (27 PyTorch architectures incl. 7
end-to-end TS variants) and the distributed MongoDB task pool
(`qlib/workflow/task/manage.py:33`) — both valuable, but the first needs torch
installed and the second needs MongoDB.
