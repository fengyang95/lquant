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


# ---------------------------------------------------------------------------
# 8. 配置模型校验 + 存储分支（覆盖率补齐：退化输入与合并/删除路径）
# ---------------------------------------------------------------------------

def test_config_model_rejections_and_field_lookup() -> None:
    from lquant.data.ext.models import PullConfig

    with pytest.raises(ExtConfigError, match="字段名不能为空"):
        ExtField("   ")
    with pytest.raises(ExtConfigError, match="缺 name"):
        ExtField.from_dict({"dtype": "float"})
    with pytest.raises(ExtConfigError, match="method 非法"):
        PullConfig.from_dict({"method": "DELETE"})
    assert PullConfig.from_dict({"timeout_seconds": 0}).timeout_seconds == 30  # 非法超时退回默认
    with pytest.raises(ExtConfigError, match="缺少显示名"):
        ExtConfig(id="a", label=" ", mode="snapshot", fields=[ExtField("a", "float")])
    with pytest.raises(ExtConfigError, match="mode 非法"):
        ExtConfig(id="a", label="x", mode="nope", fields=[ExtField("a", "float")])
    with pytest.raises(ExtConfigError, match="至少需要一个字段"):
        ExtConfig(id="a", label="x", mode="snapshot", fields=[])
    with pytest.raises(ExtConfigError, match="字段名重复"):
        ExtConfig(id="a", label="x", mode="snapshot",
                  fields=[ExtField("a", "float"), ExtField("a", "int")])
    with pytest.raises(ExtConfigError, match="date_format 非法"):
        ExtConfig(id="a", label="x", mode="snapshot", fields=[ExtField("a", "float")],
                  date_format="compact2")
    with pytest.raises(ExtConfigError, match="date_field 不能为空"):
        ExtConfig(id="a", label="x", mode="snapshot", fields=[ExtField("a", "float")],
                  date_field="   ")
    cfg = ExtConfig(id="a", label="x", mode="snapshot", fields=[ExtField("a", "float")])
    assert cfg.field("a").dtype == "float"
    assert cfg.field("ghost") is None
    with pytest.raises(ExtConfigError, match="配置缺字段"):
        ExtConfig.from_dict({"id": "a"})


def test_storage_helper_edge_branches(root: Path) -> None:
    from lquant.data.ext import storage
    from lquant.data.ext.storage import _cast_to_schema, _to_day_label

    # 目录不存在 → 空列表（不是抛错）
    assert storage.existing_dates(ts_config(), root) == []
    # 日期标签归一：空串 → None；紧凑 YYYYMMDD → ISO；ISO 时间前缀 → 日期
    assert _to_day_label("") is None
    assert _to_day_label("20240301") == "2024-03-01"
    assert _to_day_label("2024-03-01 09:30:00") == "2024-03-01"
    # 声明了 date 字段时按 Date 口径单独处理，不参与普通 cast
    cfg = ts_config(fields=[ExtField("date", "string"), ExtField("heat", "float")])
    out = _cast_to_schema(pl.DataFrame({"date": ["2024-03-01"], "heat": [1.0]}), cfg)
    assert out["date"].to_list() == ["2024-03-01"]


def test_write_frame_rejects_empty_frame(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="空数据不写入"):
        write_frame(pl.DataFrame(), cfg, day=date(2024, 3, 1), data_root=root)


def test_second_write_merges_into_existing_partition(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    n = write_json_rows(cfg, [{"symbol": "000001", "heat": 2.0, "concepts": "银行"}],
                        day=date(2024, 3, 1), data_root=root)
    assert n == 2  # 合并旧分区后两行都在
    df, _ = read_table(cfg, day=date(2024, 3, 1), data_root=root)
    assert set(df["symbol"].to_list()) == {"600000.SH", "000001.SZ"}


def test_snapshot_write_merges_by_symbol_and_keeps_last(root: Path) -> None:
    cfg = ExtConfig(id="senti", label="情绪快照", mode="snapshot",
                    fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "score": 1.0},
                          {"symbol": "000001", "score": 2.0}], data_root=root)
    n = write_json_rows(cfg, [{"symbol": "600000", "score": 9.0}], data_root=root)
    assert n == 2  # 同标的覆盖、其它标的保留
    df, active = read_table(cfg, data_root=root)
    assert active is None
    got = dict(zip(df["symbol"].to_list(), df["score"].to_list(), strict=True))
    assert got["600000.SH"] == 9.0 and got["000001.SZ"] == 2.0


def test_market_level_snapshot_keeps_only_last_row(root: Path) -> None:
    cfg = ExtConfig(id="mkt_snap", label="全市场快照", mode="snapshot", market_level=True,
                    symbol_field=None, fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"score": 1.0}], data_root=root)
    write_json_rows(cfg, [{"score": 2.0}], data_root=root)
    df, _ = read_table(cfg, data_root=root)
    assert df["score"].to_list() == [2.0]  # 无去重键 → 只留最后一行


def test_snapshot_range_query_rejected_and_empty_snapshot(root: Path) -> None:
    cfg = ExtConfig(id="senti2", label="情绪快照", mode="snapshot",
                    fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    # 无数据 → 空帧，不是抛错
    df, active = read_table(cfg, data_root=root)
    assert df.is_empty() and active is None
    with pytest.raises(ExtConfigError, match="不支持日期范围"):
        read_table(cfg, start_date=date(2024, 3, 1), end_date=date(2024, 3, 2), data_root=root)


def test_range_latest_and_missing_partition_reads(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    for d, sym in ((date(2024, 3, 1), "600000"), (date(2024, 3, 4), "000001")):
        write_json_rows(cfg, [{"symbol": sym, "heat": 1.0, "concepts": "AI"}],
                        day=d, data_root=root)

    df, label = read_table(cfg, start_date=date(2024, 3, 1), end_date=date(2024, 3, 4),
                           data_root=root)
    assert label == "2024-03-01..2024-03-04" and df.height == 2
    # 缺省读最新分区
    df_latest, latest = read_table(cfg, data_root=root)
    assert latest == "2024-03-04" and df_latest["symbol"].to_list() == ["000001.SZ"]
    # 区间里没有分区 → 空帧
    empty, none_label = read_table(cfg, start_date=date(2024, 4, 1), end_date=date(2024, 4, 2),
                                   data_root=root)
    assert empty.is_empty() and none_label is None
    # start > end 显式报错
    with pytest.raises(ExtConfigError, match="晚于"):
        read_table(cfg, start_date=date(2024, 3, 5), end_date=date(2024, 3, 1), data_root=root)
    # 有分区但请求那天没有 → 空帧 + 该日标签（不是静默给别的日期）
    miss, miss_label = read_table(cfg, day=date(2024, 3, 2), data_root=root)
    assert miss.is_empty() and miss_label == "2024-03-02"
    # 非法日期（路径穿越形状）在拼目录名之前就被拒绝
    with pytest.raises(ExtConfigError, match="不是合法日期"):
        read_table(cfg, day="../../daily", data_root=root)


def test_partition_missing_date_column_restored_from_dir_name(root: Path) -> None:
    from lquant.data.ext.storage import partition_dir

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    d = partition_dir(cfg, date(2024, 3, 1), root)
    d.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "heat": [1.0], "concepts": ["AI"]}).write_parquet(
        d / "part.parquet")

    df, _ = read_table(cfg, day=date(2024, 3, 1), data_root=root)
    assert df["date"].to_list() == [date(2024, 3, 1)]


def test_delete_table_data_keeps_config_and_is_idempotent(root: Path) -> None:
    from lquant.data.ext.storage import delete_table_data

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    snap = ExtConfig(id="senti3", label="情绪", mode="snapshot", fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(snap)
    write_json_rows(snap, [{"symbol": "600000", "score": 1.0}], data_root=root)

    delete_table_data(cfg, root)
    delete_table_data(snap, root)
    delete_table_data(cfg, root)  # 目录只剩 config.json：幂等且不报错
    assert existing_dates(cfg, root) == []
    assert (root / "ext" / "heat" / "config.json").exists()
    assert (root / "ext" / "senti3" / "config.json").exists()
    assert not (root / "ext" / "senti3" / "part.parquet").exists()


def test_atomic_write_failure_cleans_tmp_file(root: Path, monkeypatch) -> None:
    from lquant.data.ext import storage

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)

    def boom(*_a, **_kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(OSError, match="磁盘满了"):
        write_frame(pl.DataFrame({"symbol": ["600000.SH"], "heat": [1.0], "concepts": ["AI"]}),
                    cfg, day=date(2024, 3, 1), data_root=root)
    part_dir = root / "ext" / "heat" / "date=2024-03-01"
    assert not list(part_dir.glob("*.tmp"))  # 失败不留半截临时文件


def test_config_store_get_and_corrupt_and_delete_missing(root: Path) -> None:
    store = ExtConfigStore(root)
    assert store.get("heat") is None            # 尚未建表
    assert store.get("../escape") is None       # 非法 id 视为不存在（不是 500）
    assert store.delete("heat") is False        # 不存在的表删不掉
    store.save(ts_config())
    bad = root / "ext" / "heat" / "config.json"
    bad.write_text("{ 不是 json", encoding="utf-8")
    with pytest.raises(ValueError, match="配置损坏"):
        store.get("heat")
    # 一份坏配置不拖垮整张列表（只记日志跳过）
    assert store.load_all() == []


def test_query_validation_and_empty_values(root: Path) -> None:
    from lquant.data.ext.query import _json_safe, apply_filters, apply_sort, query_values

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    assert query_values(cfg, "concepts", data_root=root)["distinct"] == 0  # 空表直接返回

    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    assert _json_safe(float("nan")) is None and _json_safe(float("inf")) is None
    assert _json_safe(date(2024, 3, 1)) == "2024-03-01"

    df = pl.DataFrame({"heat": [1.0], "concepts": ["AI"]})
    for bad in ("~x", "x~", "!=x", "x!=", ":v", "heat:"):
        with pytest.raises(ExtConfigError):
            apply_filters(df, [bad])
    with pytest.raises(ExtConfigError, match="不存在"):
        apply_filters(df, ["ghost:1"])
    with pytest.raises(ExtConfigError, match="不存在"):
        apply_sort(df, "ghost:desc")
    with pytest.raises(ExtConfigError, match="offset 不能为负"):
        query_rows(cfg, offset=-1, data_root=root)
    with pytest.raises(ExtConfigError, match="limit 必须"):
        query_rows(cfg, limit=0, data_root=root)
    with pytest.raises(ExtConfigError, match="列不存在"):
        query_rows(cfg, columns=["ghost"], data_root=root)
    with pytest.raises(ExtConfigError, match="不存在"):
        query_values(cfg, "ghost", data_root=root)


def test_signal_ops_and_error_paths(root: Path) -> None:
    from lquant.data.ext.signals import evaluate_signal, signal_fields

    cfg = ts_config()
    frame = pl.DataFrame({"symbol": ["A", "B"], "ext_heat_concepts": ["AI", "银行"]})
    assert signal_fields(cfg) == ["concepts"]
    assert apply_signals(frame, cfg, []) is frame  # 无条件 = 不筛
    assert evaluate_signal(frame, cfg, {"field": "concepts", "op": "eq", "value": "AI"}).to_list() == [True, False]
    with pytest.raises(ExtConfigError, match="运算符非法"):
        evaluate_signal(frame, cfg, {"field": "concepts", "op": "gt", "value": "AI"})
    with pytest.raises(ExtConfigError, match="缺 value"):
        evaluate_signal(frame, cfg, {"field": "concepts", "op": "eq"})
    with pytest.raises(ExtConfigError, match="找不到"):
        evaluate_signal(pl.DataFrame({"symbol": ["A"]}), cfg,
                        {"field": "concepts", "op": "eq", "value": "AI"})


def test_duckdb_sync_all_drop_and_failure_isolation(root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LQ_DATA_DIR", str(root))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        from lquant.core.db import reader
        from lquant.data.ext import duckdb as ext_duck
        from lquant.data.ext.duckdb import drop_view, sync_all, sync_view

        snap = ExtConfig(id="senti4", label="情绪", mode="snapshot",
                         fields=[ExtField("score", "float")])
        ts = ts_config()
        ExtConfigStore(root).save(snap)
        ExtConfigStore(root).save(ts)
        # 无数据 → 不建视图并返回 False（snapshot 与 timeseries 两条路径）
        assert sync_view(snap, root) is False
        assert sync_view(ts, root) is False

        write_json_rows(ts, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                        day=date(2024, 3, 1), data_root=root)
        assert sync_all(root) == ["heat"]  # 无数据的 snapshot 跳过，不影响他人

        # 单表刷新失败只记日志继续（隔离）
        real = ext_duck.sync_view

        def flaky(cfg, data_root=None):
            if cfg.id == "senti4":
                raise RuntimeError("路径炸了")
            return real(cfg, data_root)

        monkeypatch.setattr(ext_duck, "sync_view", flaky)
        ExtConfigStore(root).save(ExtConfig(id="senti4", label="情绪", mode="snapshot",
                                            fields=[ExtField("score", "float")]))
        assert sync_all(root) == ["heat"]

        drop_view("heat")
        import duckdb

        with reader() as con, pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM ext_heat")
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 9. PIT 注入的分支与缓存（覆盖率补齐）
# ---------------------------------------------------------------------------

def test_pit_frame_cache_and_empty_signatures(root: Path) -> None:
    from lquant.data.ext import pit

    cfg = ts_config()
    snap = ExtConfig(id="snapx", label="快照", mode="snapshot",
                     fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    ExtConfigStore(root).save(snap)
    # 无数据：snapshot 无文件 / timeseries 无分区 → 空帧（空签名分支）
    assert pit._ext_frame(cfg, root).is_empty()
    assert pit._ext_frame(snap, root).is_empty()

    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    first = pit._ext_frame(cfg, root)
    assert pit._ext_frame(cfg, root) is first  # 命中帧缓存（签名未变）
    pit.invalidate_frame_cache(root)
    assert not pit._ext_frame(cfg, root).is_empty()
    pit.invalidate_frame_cache()  # 无参 = 全清


def test_pit_panel_without_date_column_skips_timeseries(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    panel = pl.DataFrame({"symbol": ["600000.SH"], "heat": [1.0]})  # 没有日期列
    out = attach_ext_columns(panel, data_root=root)
    assert "ext_heat_heat" not in out.columns


def test_pit_snapshot_skip_and_market_level(root: Path) -> None:
    snap = ExtConfig(id="sx", label="快照", mode="snapshot", fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(snap)
    write_json_rows(snap, [{"symbol": "600000", "score": 1.0}], data_root=root)
    single = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)]})
    # include_snapshot=False：回测里即便单日也强制跳过
    assert "ext_sx_score" not in attach_ext_columns(
        single, data_root=root, include_snapshot=False).columns
    assert attach_ext_columns(single, data_root=root)["ext_sx_score"].to_list() == [1.0]

    mkt = ExtConfig(id="msx", label="市场快照", mode="snapshot", market_level=True,
                    symbol_field=None, fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(mkt)
    write_json_rows(mkt, [{"score": 0.5}], data_root=root)
    # 市场级快照无标的列 → 不按标的 join（避免笛卡尔积）
    assert "ext_msx_score" not in attach_ext_columns(single, data_root=root).columns


def test_pit_timeseries_symbol_missing_and_existing_column(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    # 面板没有 symbol 列 → 该表跳过（不是 join 到 null）
    assert "ext_heat_heat" not in attach_ext_columns(
        pl.DataFrame({"trade_date": [date(2024, 3, 1)]}), data_root=root).columns
    # 面板已有同名列 → 不重复注入（保留调用方已有值）
    panel = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)],
                          "ext_heat_heat": [42.0]})
    assert attach_ext_columns(panel, data_root=root)["ext_heat_heat"].to_list() == [42.0]


def test_pit_explicit_date_column(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)
    panel = pl.DataFrame({"symbol": ["600000.SH"], "asof": [date(2024, 3, 1)]})
    assert attach_ext_columns(panel, data_root=root, date_column="asof")[
        "ext_heat_heat"].to_list() == [1.0]
    # 显式指定的列不存在 → 退化成「没有日期列」，该表跳过
    assert "ext_heat_heat" not in attach_ext_columns(
        panel, data_root=root, date_column="ghost").columns


def test_pit_restores_date_from_partition_dir(root: Path) -> None:
    from lquant.data.ext.storage import partition_dir

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    d = partition_dir(cfg, date(2024, 3, 1), root)
    d.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "heat": [1.0], "concepts": ["AI"]}).write_parquet(
        d / "part.parquet")
    panel = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)]})
    assert attach_ext_columns(panel, data_root=root)["ext_heat_heat"].to_list() == [1.0]


def test_pit_single_table_failure_is_isolated(root: Path, monkeypatch) -> None:
    from lquant.data.ext import pit

    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    write_json_rows(cfg, [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
                    day=date(2024, 3, 1), data_root=root)

    def boom(*_a, **_kw):
        raise RuntimeError("单表炸了")

    monkeypatch.setattr(pit, "_attach_timeseries", boom)
    panel = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)]})
    out = attach_ext_columns(panel, data_root=root)  # 单表失败只跳过，不炸整帧
    assert "ext_heat_heat" not in out.columns


def test_pit_snapshot_empty_frame_and_existing_column(root: Path) -> None:
    from lquant.data.ext import pit

    cfg = ExtConfig(id="se", label="快照", mode="snapshot", fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    single = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)]})
    # 配置在但没有数据 → 直接原样返回
    assert "ext_se_score" not in attach_ext_columns(single, data_root=root).columns

    write_json_rows(cfg, [{"symbol": "600000", "score": 1.0}], data_root=root)
    with_col = single.with_columns(pl.lit(7.0).alias("ext_se_score"))
    assert attach_ext_columns(with_col, data_root=root)["ext_se_score"].to_list() == [7.0]
    assert pit._attach_snapshot(single, cfg, root)["ext_se_score"].to_list() == [1.0]


# ---------------------------------------------------------------------------
# 10. 拉取/上传路径的解析与分页分支（覆盖率补齐）
# ---------------------------------------------------------------------------

def test_extract_rows_paths_and_errors() -> None:
    from lquant.data.ext.ingest import extract_rows

    assert extract_rows([{"a": 1}], "") == [{"a": 1}]
    assert extract_rows({"data": {"list": [1]}}, "data.list") == [1]
    assert extract_rows({"data": [[{"x": 1}]]}, "data.0") == [{"x": 1}]  # 路径里走数组下标
    with pytest.raises(ExtConfigError, match="不是数组"):
        extract_rows({"a": 1}, "")
    with pytest.raises(ExtConfigError, match="缺键"):
        extract_rows({}, "data.list")
    with pytest.raises(ExtConfigError, match="解析失败"):
        extract_rows({"data": []}, "data.5")
    with pytest.raises(ExtConfigError, match="中间值"):
        extract_rows({"data": 1}, "data.list")
    with pytest.raises(ExtConfigError, match="指向的不是数组"):
        extract_rows({"data": {"list": {"x": 1}}}, "data.list")


def test_apply_field_map_and_build_url() -> None:
    from lquant.data.ext.ingest import apply_field_map, build_url

    assert apply_field_map([{"a": 1}], {}) == [{"a": 1}]
    assert apply_field_map([{"a": 1, "b": 2}], {"a": "x"}) == [{"x": 1, "b": 2}]

    cfg = ts_config(pull=PullConfig(url="https://e.test/x"))
    assert build_url(cfg, date(2024, 3, 1)) == "https://e.test/x"  # 无参数原样返回

    cfg2 = ts_config(date_param="date", pull=PullConfig(url="https://e.test/x?date=old&k=1"))
    url = build_url(cfg2, date(2024, 3, 1), {"page": 2})
    assert "date=2024-03-01" in url and "date=old" not in url and "k=1" in url
    assert "page=2" in url

    cfg3 = ts_config(date_param="d", date_format="compact",
                     pull=PullConfig(url="https://e.test/x"))
    assert build_url(cfg3, date(2024, 3, 1)).endswith("d=20240301")


def test_assert_rows_date_edges() -> None:
    from lquant.data.ext.ingest import _day_label, assert_rows_date

    cfg = ts_config()
    assert_rows_date([{"symbol": "600000"}], cfg, date(2024, 3, 1))  # 无日期字段的行跳过
    assert_rows_date([{"date": "20240301", "symbol": "x"}], cfg, date(2024, 3, 1))  # 紧凑格式
    assert _day_label("") is None
    with pytest.raises(ExtDateMismatch):
        assert_rows_date([{"date": ""}], cfg, date(2024, 3, 1))  # 认不出的日期拒绝该分区


def test_fetch_rows_requires_url_and_simple_fetcher(root: Path) -> None:
    cfg = ts_config()
    with pytest.raises(ExtConfigError, match="未配置拉取 URL"):
        fetch_rows(cfg, date(2024, 3, 1))

    cfg2 = ts_config(date_param="date", pull=PullConfig(url="https://e.test/h"))
    ExtConfigStore(root).save(cfg2)

    def only_url(url):  # 最简 fetcher：只接受 url（触发 kwargs 调用的 TypeError 回退）
        return [{"date": "2024-03-01", "symbol": "600000", "heat": 1.0, "concepts": "AI"}]

    assert len(fetch_rows(cfg2, date(2024, 3, 1), fetcher=only_url)) == 1


def test_fetch_rows_pagination_short_page_and_max_pages(root: Path) -> None:
    cfg = ts_config(date_param="date", pull=PullConfig(
        url="https://e.test/h", page_param="p", page_size_param="n", page_size=2, max_pages=5))
    ExtConfigStore(root).save(cfg)

    def short_page(url, **_kw):
        page = int(url.split("p=")[1].split("&")[0])
        n = 2 if page == 1 else 1  # 第二页短页 → 停
        return [{"date": "2024-03-01", "symbol": f"60000{i}", "heat": 1.0, "concepts": "AI"}
                for i in range(n)]

    rows = fetch_rows(cfg, date(2024, 3, 1), fetcher=short_page)
    assert len(rows) == 3

    capped = ts_config(id="cap", date_param="date", pull=PullConfig(
        url="https://e.test/h", page_param="p", max_pages=2))
    ExtConfigStore(root).save(capped)
    calls = []

    def always_one(url, **_kw):
        calls.append(url)
        return [{"date": "2024-03-01", "symbol": "600000", "heat": 1.0, "concepts": "AI"}]

    assert len(fetch_rows(capped, date(2024, 3, 1), fetcher=always_one)) == 2  # 达到 max_pages 停
    assert len(calls) == 2  # 两次请求都发出后才判定超限


def test_pull_day_writes_and_rejects_empty(root: Path) -> None:
    from lquant.data.ext.ingest import pull_day

    cfg = ts_config(date_param="date", pull=PullConfig(url="https://e.test/h"))
    ExtConfigStore(root).save(cfg)
    row = {"date": "2024-03-01", "symbol": "600000", "heat": 1.0, "concepts": "AI"}
    assert pull_day(cfg, date(2024, 3, 1), data_root=root,
                    fetcher=lambda url, **_k: [row]) == 1
    with pytest.raises(ExtConfigError, match="拉取到 0 行"):
        pull_day(cfg, date(2024, 3, 2), data_root=root, fetcher=lambda url, **_k: [])


def test_backfill_rejects_bad_config_and_ranges(root: Path) -> None:
    snap = ExtConfig(id="bf_snap", label="快照", mode="snapshot",
                     fields=[ExtField("score", "float")])
    with pytest.raises(ExtConfigError, match="只有 timeseries"):
        backfill(snap, date(2024, 3, 1), date(2024, 3, 2), data_root=root,
                 trading_days=[date(2024, 3, 1)])
    cfg = ts_config()
    with pytest.raises(ExtConfigError, match="未配置拉取 URL"):
        backfill(cfg, date(2024, 3, 1), date(2024, 3, 2), data_root=root,
                 trading_days=[date(2024, 3, 1)])
    cfg2 = ts_config(date_param="date", pull=PullConfig(url="https://e.test/h"))
    with pytest.raises(ExtConfigError, match="晚于"):
        backfill(cfg2, date(2024, 3, 3), date(2024, 3, 1), data_root=root,
                 trading_days=[date(2024, 3, 1)])
    with pytest.raises(ExtConfigError, match="上限"):
        backfill(cfg2, date(2024, 1, 1), date(2024, 6, 1), data_root=root,
                 trading_days=[date(2024, 1, 2)], max_days=10)
    with pytest.raises(ExtConfigError, match="没有交易日"):
        backfill(cfg2, date(2024, 3, 1), date(2024, 3, 5), data_root=root,
                 trading_days=[date(2024, 4, 1)])


def test_backfill_counts_empty_days_and_sleeps(root: Path) -> None:
    cfg = ts_config(date_param="date", pull=PullConfig(url="https://e.test/h"))
    ExtConfigStore(root).save(cfg)
    report = backfill(cfg, date(2024, 3, 1), date(2024, 3, 4), data_root=root,
                      trading_days=[date(2024, 3, 1), date(2024, 3, 4)],
                      fetcher=lambda url, **_k: [], sleep_seconds=0.001)
    assert report["empty"] == 2 and report["fetched"] == 0


def test_import_file_aliases_bad_format_and_missing(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    csv = root / "u.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    # 日期列用别名「日期」→ 归一到 config.date_field
    csv.write_text("symbol,heat,concepts,日期\n600000,1.0,AI,2024-03-01\n", encoding="utf-8")
    assert import_file(cfg, csv, day=date(2024, 3, 1), data_root=root) == 1
    with pytest.raises(ExtConfigError, match="必须指定 --date"):
        import_file(cfg, csv, data_root=root)
    txt = root / "u.txt"
    txt.write_text("x", encoding="utf-8")
    with pytest.raises(ExtConfigError, match="不支持的文件格式"):
        import_file(cfg, txt, day=date(2024, 3, 1), data_root=root)
    with pytest.raises(ExtConfigError, match="文件不存在"):
        import_file(cfg, root / "nope.csv", data_root=root)


def test_import_json_list_and_bad_top_level(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    arr = root / "arr.json"
    arr.parent.mkdir(parents=True, exist_ok=True)
    arr.write_text(json.dumps([{"symbol": "600000", "heat": 1.0, "concepts": "AI"}]),
                   encoding="utf-8")
    assert import_file(cfg, arr, day=date(2024, 3, 1), data_root=root) == 1
    bad = root / "bad.json"
    bad.write_text(json.dumps("字符串"), encoding="utf-8")
    with pytest.raises(ExtConfigError, match="必须是数组"):
        import_file(cfg, bad, day=date(2024, 3, 1), data_root=root)


def test_import_snapshot_without_day_uses_today(root: Path) -> None:
    cfg = ExtConfig(id="snap_import", label="快照", mode="snapshot",
                    fields=[ExtField("score", "float")])
    ExtConfigStore(root).save(cfg)
    csv = root / "snap.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text("symbol,score\n600000,1.0\n", encoding="utf-8")
    assert import_file(cfg, csv, data_root=root) == 1


def test_write_json_rows_row_shape_errors(root: Path) -> None:
    cfg = ts_config()
    ExtConfigStore(root).save(cfg)
    with pytest.raises(ExtConfigError, match="不是对象"):
        write_json_rows(cfg, ["not-a-dict"], day=date(2024, 3, 1), data_root=root)
    with pytest.raises(ExtConfigError, match="缺少标的字段"):
        write_json_rows(cfg, [{"heat": 1.0, "concepts": "AI"}], day=date(2024, 3, 1),
                        data_root=root)


# ---------------------------------------------------------------------------
# 11. schema 归一/编码与 ext_bridge 缓存（覆盖率补齐）
# ---------------------------------------------------------------------------

def test_schema_dtype_and_symbol_normalization() -> None:
    from decimal import Decimal

    from lquant.data.ext.schema import (
        clean_column_names,
        infer_dtype,
        normalize_symbol,
        normalize_symbol_column,
        polars_dtype,
    )

    with pytest.raises(ValueError, match="未知字段类型"):
        polars_dtype("decimal")
    assert polars_dtype("float") is pl.Float64
    # Decimal 等数值类型统一归 float（不能因为不认识就退成 string）
    assert infer_dtype(pl.Series([Decimal("1.5")])) == "float"
    assert infer_dtype(pl.Series([object()])) == "string"

    # 重名列显式加序号消歧，而不是让 polars 抛 DuplicateError
    dup = pl.DataFrame([(1, 2)], schema=["a", "a(1)"], orient="row")
    assert clean_column_names(dup).columns == ["a", "a_1"]

    assert normalize_symbol(None) == ""
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("不是代码") == "不是代码"  # 解析失败原样返回，不静默丢
    assert normalize_symbol_column(pl.DataFrame({"x": [1]}), "symbol").columns == ["x"]


def test_ensure_utf8_csv_transcode_failure_paths(tmp_path: Path) -> None:
    from lquant.data.ext.schema import _transcode_to_utf8, ensure_utf8_csv

    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"\xff\xff\xff")  # 任何中文编码都解不开
    dst = tmp_path / "bad.utf8"
    assert _transcode_to_utf8(bad, dst, "gbk") is False
    assert not dst.exists()  # 半成品必须被删掉

    assert ensure_utf8_csv(bad) == bad  # 全部编码都失败 → 交回原文件
    ok = tmp_path / "ok.csv"
    ok.write_text("symbol,heat\n600000,1.0\n", encoding="utf-8")
    assert ensure_utf8_csv(ok) == ok


def test_ext_bridge_catalog_and_skip_cache(root: Path, tmp_path: Path, monkeypatch) -> None:
    from lquant.factors import ext_bridge

    # 配置根是文件（不是目录）→ 签名读不出来时退化成空签名，不抛
    (root / "ext").parent.mkdir(parents=True, exist_ok=True)
    (root / "ext").write_text("not a dir", encoding="utf-8")
    assert ext_bridge._config_signature(root) == ()
    (root / "ext").unlink()

    ExtConfigStore(root).save(ts_config())
    assert ext_bridge.ext_factor_ids(root) == frozenset({"ext_heat_heat"})
    catalog = ext_bridge.ext_field_catalog(root)
    assert [f["name"] for f in catalog["factors"]] == ["ext_heat_heat"]
    assert [s["column"] for s in catalog["string_fields"]] == ["ext_heat_concepts"]

    monkeypatch.setenv("LQ_DATA_DIR", str(root))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        first = ext_bridge.ensure_synced(root)
        assert first["registered"] == 1
        again = ext_bridge.sync_ext_factors(root)  # 同签名 → 跳过写库
        assert again["skipped"] is True and again["registered"] == 1
        ext_bridge.invalidate_ext_caches(root)
        assert ext_bridge.sync_ext_factors(root)["skipped"] is False
    finally:
        get_settings.cache_clear()
