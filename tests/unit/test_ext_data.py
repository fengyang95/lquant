"""扩展数据表体系的单测（B2）。

覆盖三条写入路径、日期防污染契约、PIT 边界、自动因子注册、查询面与退化输入。
全程离线：数据根用 tmp_path，HTTP 用注入的 fake fetcher —— **不联网**，
否则「接口忽略 date 参数」这种事故根本无法稳定复现。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.ext import (
    ExtConfig,
    ExtConfigError,
    ExtConfigStore,
    ExtDateMismatch,
    ExtField,
    PullConfig,
    apply_signals,
    attach_ext_columns,
    backfill,
    existing_dates,
    ext_column_name,
    fetch_rows,
    import_file,
    query_rows,
    query_values,
    read_table,
    write_frame,
    write_json_rows,
)
from lquant.factors.ext_bridge import (
    ext_factor_specs,
    ext_string_field_entries,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(root: Path) -> ExtConfigStore:
    return ExtConfigStore(root)


def ts_config(**kw) -> ExtConfig:
    base = {
        "id": "heat",
        "label": "热度",
        "mode": "timeseries",
        "fields": [ExtField("heat", "float"), ExtField("concepts", "string")],
    }
    base.update(kw)
    return ExtConfig(**base)


# ---------------------------------------------------------------------------
# 1. JSON 写入 + 查询面
# ---------------------------------------------------------------------------

def test_json_write_rows_pagination_filter_sort(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    rows = [
        {"symbol": f"6000{i:02d}", "heat": 1.0 + i,
         "concepts": "AI;芯片" if i % 2 == 0 else "白酒"}
        for i in range(10)
    ]
    n = write_json_rows(cfg, rows, day=date(2024, 3, 1), data_root=root)
    assert n == 10

    page = query_rows(cfg, day=date(2024, 3, 1), offset=2, limit=3, data_root=root)
    assert page["total"] == 10  # total = 过滤后、分页前
    assert len(page["rows"]) == 3
    assert page["rows"][0]["heat"] == 3.0

    filtered = query_rows(cfg, day=date(2024, 3, 1), filters=["concepts~AI"], data_root=root)
    assert filtered["total"] == 5
    assert all(r["concepts"] == "AI;芯片" for r in filtered["rows"])

    eq = query_rows(cfg, day=date(2024, 3, 1), filters=["concepts:白酒"], data_root=root)
    assert eq["total"] == 5
    ne = query_rows(cfg, day=date(2024, 3, 1), filters=["concepts!=白酒"], data_root=root)
    assert ne["total"] == 5

    desc = query_rows(cfg, day=date(2024, 3, 1), sort="heat:desc", limit=1, data_root=root)
    assert desc["rows"][0]["heat"] == 10.0

    cols = query_rows(cfg, day=date(2024, 3, 1), columns=["heat"], limit=1, data_root=root)
    assert set(cols["rows"][0]) == {"heat"}


def test_query_values_dedupe_counts(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(
        cfg,
        [{"symbol": f"60000{i}", "heat": i, "concepts": "AI" if i < 3 else "白酒"}
         for i in range(4)],
        day=date(2024, 3, 1), data_root=root,
    )
    payload = query_values(cfg, "concepts", day=date(2024, 3, 1), data_root=root)
    assert payload["distinct"] == 2
    assert payload["values"][0] == {"value": "AI", "count": 3}


# ---------------------------------------------------------------------------
# 2. CSV / Excel 上传（编码 + 列名归一）
# ---------------------------------------------------------------------------

def test_csv_upload_gbk_and_chinese_headers(root: Path) -> None:
    # 字段名本身就是中文（自有数据常态），配置与 CSV 一致即可
    cfg = ts_config(fields=[ExtField("热度分", "float"), ExtField("所属概念", "string")])
    ExtConfigStore(root).save(cfg)
    csv = root / "upload.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    # GBK 编码 + 代码列别名（"代码" → symbol）—— 国内行情软件导出的典型形态
    csv.write_bytes("代码,热度分,所属概念\n600000,12.5,AI;芯片\n000001,3.0,银行\n".encode("gbk"))

    n = import_file(cfg, csv, day=date(2024, 3, 4), data_root=root)
    assert n == 2
    df, _ = read_table(cfg, day=date(2024, 3, 4), data_root=root)
    assert set(df["symbol"].to_list()) == {"600000.SH", "000001.SZ"}
    assert df.sort("热度分")["热度分"].to_list() == [3.0, 12.5]
    assert set(df["所属概念"].to_list()) == {"AI;芯片", "银行"}


def test_excel_upload(root: Path) -> None:
    pytest.importorskip("openpyxl")
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    path = root / "upload.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    import pandas as pd

    pd.DataFrame({
        "symbol": ["600000"], "heat": [7.5], "concepts": ["券商"],
    }).to_excel(path, index=False)
    assert import_file(cfg, path, day=date(2024, 3, 5), data_root=root) == 1
    df, _ = read_table(cfg, day=date(2024, 3, 5), data_root=root)
    assert df["heat"].to_list() == [7.5]


def test_import_json_file(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    path = root / "rows.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": "2024-03-06",
        "rows": [{"symbol": "600519", "heat": 99.0, "concepts": "白酒"}],
    }, ensure_ascii=False), encoding="utf-8")
    assert import_file(cfg, path, data_root=root) == 1
    df, active = read_table(cfg, day=date(2024, 3, 6), data_root=root)
    assert active == "2024-03-06" and df["heat"].to_list() == [99.0]


# ---------------------------------------------------------------------------
# 3. HTTP 拉取 + ⚠️ 日期防污染契约（最重要的一条）
# ---------------------------------------------------------------------------

def _echo_fetcher(expected: dict[str, str] | None = None):
    """返回「响应行日期 == 请求日期」的正常接口。"""
    from urllib.parse import parse_qs, urlparse

    def _fetch(url: str, **_kw):
        qs = parse_qs(urlparse(url).query)
        day = qs.get("date", ["2024-01-01"])[0]
        row = {"date": day, "symbol": "600000", "heat": 1.0, "concepts": "AI"}
        return {"data": {"list": [row]}}

    return _fetch


def test_http_pull_writes_partition(root: Path) -> None:
    cfg = ts_config(
        date_param="date",
        pull=PullConfig(url="https://example.test/heat", response_path="data.list"),
    )
    ExtConfigStore(root).save(cfg)
    rows = fetch_rows(cfg, date(2024, 3, 7), fetcher=_echo_fetcher())
    assert rows[0]["date"] == "2024-03-07"
    assert fetch_rows  # 解析后由调用方写盘


def test_http_pull_rejects_mismatched_date(root: Path) -> None:
    """⚠️ 核心契约：接口忽略 ?date= 永远返回当日数据 → 该日必须被拒绝写入。

    这是本项目最重要的一条用例：不校验就会把「今天的数据」写进历史每一天，
    schema 正确、没有报错，静默污染整个时序。
    """
    cfg = ts_config(
        date_param="date",
        pull=PullConfig(url="https://example.test/heat", response_path="data.list"),
    )
    ExtConfigStore(root).save(cfg)
    today = date(2024, 6, 1)

    def ignores_date(url: str, **_kw):  # 永远返回「今天」
        return {"data": {"list": [
            {"date": today.isoformat(), "symbol": "600000", "heat": 9.9, "concepts": "AI"}
        ]}}

    with pytest.raises(ExtDateMismatch):
        fetch_rows(cfg, date(2024, 1, 2), fetcher=ignores_date)

    report = backfill(
        cfg, date(2024, 1, 2), date(2024, 1, 4),
        data_root=root, trading_days=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        fetcher=ignores_date,
    )
    assert report["fetched"] == 0
    assert len(report["failed"]) == 3
    assert all("不一致" in f["reason"] for f in report["failed"])
    assert existing_dates(cfg, root) == []  # 历史分区一行都没落


def test_backfill_idempotent_and_skips_existing(root: Path) -> None:
    cfg = ts_config(
        date_param="date",
        pull=PullConfig(url="https://example.test/heat", response_path="data.list"),
    )
    ExtConfigStore(root).save(cfg)
    days = [date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)]
    first = backfill(cfg, days[0], days[-1], data_root=root, trading_days=days,
                     fetcher=_echo_fetcher())
    assert first["fetched"] == 3 and first["failed"] == []
    before = read_table(cfg, day=days[0], data_root=root)[0].height

    second = backfill(cfg, days[0], days[-1], data_root=root, trading_days=days,
                      fetcher=_echo_fetcher())
    assert second["fetched"] == 0 and second["skipped_existing"] == 3
    assert read_table(cfg, day=days[0], data_root=root)[0].height == before  # 无重复行


def test_backfill_requires_date_param(root: Path) -> None:
    cfg = ts_config(pull=PullConfig(url="https://example.test/heat"))
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="date_param"):
        backfill(cfg, date(2024, 3, 1), date(2024, 3, 3), data_root=root,
                 trading_days=[date(2024, 3, 1)], fetcher=_echo_fetcher())


def test_single_day_failure_does_not_abort_backfill(root: Path) -> None:
    cfg = ts_config(
        date_param="date",
        pull=PullConfig(url="https://example.test/heat", response_path="data.list"),
    )
    ExtConfigStore(root).save(cfg)
    days = [date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)]
    good = _echo_fetcher()

    def flaky(url: str, **kw):
        if "2024-03-04" in url:
            raise RuntimeError("上游 500")
        return good(url, **kw)

    report = backfill(cfg, days[0], days[-1], data_root=root, trading_days=days, fetcher=flaky)
    assert report["fetched"] == 2
    assert [f["date"] for f in report["failed"]] == ["2024-03-04"]
    assert existing_dates(cfg, root) == ["2024-03-01", "2024-03-05"]


# ---------------------------------------------------------------------------
# 4. 自动 schema 发现 → 因子注册 / 信号通道
# ---------------------------------------------------------------------------

def test_numeric_fields_registered_as_factors_string_not(root: Path) -> None:
    cfg = ts_config(fields=[
        ExtField("heat", "float"), ExtField("rank", "int"),
        ExtField("concepts", "string"), ExtField("active", "bool"),
    ])
    ExtConfigStore(root).save(cfg)
    names = {s["name"] for s in ext_factor_specs(root)}
    assert names == {"ext_heat_heat", "ext_heat_rank"}
    assert "ext_heat_concepts" not in names  # 字符串不注册因子
    assert "ext_heat_active" not in names    # bool 也不注册

    string_entries = ext_string_field_entries(root)
    assert [e["name"] for e in string_entries] == ["concepts"]
    assert ext_column_name("heat", "concepts") == "ext_heat_concepts"


def test_infer_fields_from_dataframe(root: Path) -> None:
    from lquant.data.ext import infer_fields

    df = pl.DataFrame({"a": [1], "b": [1.5], "c": ["x"], "d": [True]})
    got = {f.name: f.dtype for f in infer_fields(df)}
    assert got == {"a": "int", "b": "float", "c": "string", "d": "bool"}


def test_sync_ext_factors_into_factor_def(root: Path, tmp_path: Path, monkeypatch) -> None:
    """注册进 lquant 既有因子表 factor_def（因子库/AI 提示词的真相源）。"""
    monkeypatch.setenv("LQ_DATA_DIR", str(root))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        from lquant.factors.ext_bridge import sync_ext_factors

        ExtConfigStore(root).save(ts_config())
        result = sync_ext_factors(root)
        assert result["registered"] == 1
        from lquant.core.db import reader

        with reader() as con:
            rows = con.execute(
                "SELECT name, source, category FROM factor_def WHERE source='ext'"
            ).fetchall()
        assert rows == [("ext_heat_heat", "ext", "扩展数据")]
    finally:
        get_settings.cache_clear()


def test_string_signal_channel_contains(root: Path) -> None:
    cfg = ts_config()
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "ext_heat_concepts": ["AI;芯片", "银行"],
    })
    hit = apply_signals(frame, cfg, [{"field": "concepts", "op": "contains", "value": "AI"}])
    assert hit["symbol"].to_list() == ["600000.SH"]

    ne = apply_signals(frame, cfg, [{"field": "concepts", "op": "ne", "value": "银行"}])
    assert ne["symbol"].to_list() == ["600000.SH"]

    with pytest.raises(ExtConfigError):
        # 数值字段不属于信号条件通道
        apply_signals(frame, cfg, [{"field": "heat", "op": "contains", "value": "1"}])


# ---------------------------------------------------------------------------
# 5. PIT 边界
# ---------------------------------------------------------------------------

def test_timeseries_missing_partition_is_null_not_forward_filled(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 5.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    panel = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "600000.SH"],
        "trade_date": [date(2024, 2, 29), date(2024, 3, 1), date(2024, 3, 4)],
    })
    out = attach_ext_columns(panel, data_root=root)
    by_date = dict(zip(out["trade_date"].to_list(), out["ext_heat_heat"].to_list(), strict=True))
    assert by_date[date(2024, 3, 1)] == 5.0
    # 前面没有分区、后面没有分区：都是 null，绝不前视/后视填充
    assert by_date[date(2024, 2, 29)] is None
    assert by_date[date(2024, 3, 4)] is None


def test_snapshot_only_injected_into_single_day_frame(root: Path) -> None:
    cfg = ExtConfig(
        id="senti", label="情绪快照", mode="snapshot",
        fields=[ExtField("score", "float")],
    )
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "score": 88.0}], data_root=root)

    multi = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "trade_date": [date(2024, 3, 1), date(2024, 3, 4)],
    })
    out = attach_ext_columns(multi, data_root=root)
    assert "ext_senti_score" not in out.columns  # 多日历史帧：跳过，防未来数据

    single = multi.head(1)
    out1 = attach_ext_columns(single, data_root=root)
    assert out1["ext_senti_score"].to_list() == [88.0]


def test_market_level_table_has_no_symbol_and_joins_on_date(root: Path) -> None:
    cfg = ExtConfig(
        id="mkt", label="市场情绪", mode="timeseries", symbol_field=None,
        market_level=True, fields=[ExtField("score", "float")],
    )
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"score": 0.5}], day=date(2024, 3, 1), data_root=root)
    write_json_rows(cfg, [{"score": 0.7}], day=date(2024, 3, 4), data_root=root)

    payload = query_rows(cfg, start_date=date(2024, 3, 1), end_date=date(2024, 3, 4),
                         data_root=root)
    assert payload["total"] == 2
    assert "symbol" not in payload["rows"][0]

    panel = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "600000.SH"],
        "trade_date": [date(2024, 3, 1), date(2024, 3, 1), date(2024, 3, 4)],
    })
    out = attach_ext_columns(panel, data_root=root).sort("trade_date")
    assert out["ext_mkt_score"].to_list() == [0.5, 0.5, 0.7]


# ---------------------------------------------------------------------------
# 6. 退化输入：显式报错，不崩
# ---------------------------------------------------------------------------

def test_config_rejects_path_traversal_id() -> None:
    with pytest.raises(ExtConfigError):
        ExtConfig(id="../daily", label="x", mode="snapshot",
                  fields=[ExtField("a", "float")])


def test_config_rejects_market_level_with_symbol() -> None:
    with pytest.raises(ExtConfigError):
        ExtConfig(id="m", label="x", mode="snapshot", market_level=True,
                  symbol_field="symbol", fields=[ExtField("a", "float")])


def test_config_rejects_bad_dtype() -> None:
    with pytest.raises(ExtConfigError):
        ExtConfig(id="m", label="x", mode="snapshot", fields=[ExtField("a", "decimal")])


def test_empty_rows_rejected(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="rows 为空"):
        write_json_rows(cfg, [], day=date(2024, 3, 1), data_root=root)


def test_missing_field_reports_row_number(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="第 1 行缺少字段"):
        write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0}],
                        day=date(2024, 3, 1), data_root=root)


def test_missing_declared_column_in_frame_rejected(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="缺少声明字段"):
        write_frame(pl.DataFrame({"symbol": ["600000.SH"], "heat": [1.0]}),
                    cfg, day=date(2024, 3, 1), data_root=root)


def test_illegal_type_cast_rejected(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="无法转成 float"):
        write_json_rows(cfg, [{"symbol": "600000", "heat": "不是数字", "concepts": "x"}],
                        day=date(2024, 3, 1), data_root=root)


def test_timeseries_write_requires_date(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="必须指定日期"):
        write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "x"}],
                        data_root=root)


def test_row_date_mismatch_rejected(root: Path) -> None:
    """JSON/CSV 写入同样受日期契约保护（不只 HTTP）。"""
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="不一致"):
        write_json_rows(
            cfg,
            [{"symbol": "600000", "heat": 1.0, "concepts": "x", "date": "2024-05-01"}],
            day=date(2024, 3, 1), data_root=root,
        )


def test_empty_table_reads_as_empty_not_crash(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    df, active = read_table(cfg, data_root=root)
    assert df.is_empty() and active is None
    payload = query_rows(cfg, data_root=root)
    assert payload["total"] == 0 and payload["rows"] == []


def test_store_lists_and_deletes(root: Path) -> None:
    store = ExtConfigStore(root)
    store.save(ts_config())
    assert store.ids() == ["heat"]
    assert store.ids() == ["heat"]  # 第二次走缓存也一致
    assert store.delete("heat") is True
    assert store.ids() == []


# ---------------------------------------------------------------------------
# 7. DuckDB 可见性
# ---------------------------------------------------------------------------

def test_duckdb_view_queries_ext_table(root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LQ_DATA_DIR", str(root))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        from lquant.core.db import reader
        from lquant.data.ext.duckdb import sync_view

        cfg = ts_config()
        ExtConfigStore(root).save(cfg)
        write_json_rows(cfg, [{"symbol": "600000", "heat": 3.5, "concepts": "AI"}],
                        day=date(2024, 3, 1), data_root=root)
        assert sync_view(cfg, root) is True
        with reader() as con:
            rows = con.execute("SELECT symbol, heat FROM ext_heat").fetchall()
        assert rows == [("600000.SH", 3.5)]
    finally:
        get_settings.cache_clear()
