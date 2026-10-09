"""三种写入路径：JSON 行 / CSV·Excel 上传 / HTTP 按日拉取。

三者共用同一条落盘链（storage.write_frame），保证「同一份数据从哪个入口进
都是同一个 schema、同一套日期校验」。分头实现是这类系统的经典分叉点：
上传路径校验了日期，HTTP 路径忘了，于是只有一条入口会污染历史。

HTTP 拉取刻意只用标准库 ``urllib``：``httpx`` 在 lquant 里是 dev/server extra，
核心安装（``pip install lquant``）没有它。扩展数据的拉取是核心能力，不能
因为少装一个可选依赖就整条链路不可用。同时把 ``fetcher`` 做成可注入参数 ——
单测必须能在**不联网**的前提下复现「接口忽略 date 参数」这种真实事故。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date as _date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import polars as pl
from loguru import logger

from lquant.data.ext.models import ExtConfig, ExtConfigError
from lquant.data.ext.schema import clean_column_names, ensure_utf8_csv
from lquant.data.ext.storage import existing_dates, write_frame

#: 常见标的列名别名 → 归一目标。用户导出文件五花八门，不归一就永远缺列。
_SYMBOL_ALIASES = ("symbol", "code", "代码", "股票代码", "标的", "标的代码", "证券代码")
#: 常见日期列名别名。
_DATE_ALIASES = ("date", "日期", "trade_date", "交易日期", "时间")

#: 单次回补的日期上限：同步端点逐日请求，无上限会让一个误传的十年区间
#: 把 CLI/HTTP 请求挂死。分段执行是显式的、用户可控的。
MAX_BACKFILL_DAYS = 120


class ExtDateMismatch(ExtConfigError):
    """响应行的日期 ≠ 请求日期 —— 拒绝写入该分区（防历史污染）。"""


# ---------------------------------------------------------------------------
# 拉取响应解析
# ---------------------------------------------------------------------------

def extract_rows(payload: Any, response_path: str) -> list[dict]:
    """按 dot-path 从 JSON 响应里取行数组（空 path = 响应本身即数组）。"""
    if not response_path:
        if isinstance(payload, list):
            return payload
        raise ExtConfigError(f"response_path 为空但响应不是数组，而是 {type(payload)}")
    current = payload
    for key in response_path.split("."):
        if isinstance(current, dict):
            if key not in current:
                raise ExtConfigError(f"响应中不存在路径 {response_path!r}（缺键 {key!r}）")
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError) as e:
                raise ExtConfigError(f"响应路径 {response_path!r} 解析失败: {e}") from e
        else:
            raise ExtConfigError(f"响应路径 {response_path!r} 中间值不是 dict/list")
    if not isinstance(current, list):
        raise ExtConfigError(f"路径 {response_path!r} 指向的不是数组，而是 {type(current)}")
    return current


def apply_field_map(rows: list[dict], field_map: dict[str, str]) -> list[dict]:
    """外部字段名 → 配置字段名。``field_map``: {外部名: 内部名}。"""
    if not field_map:
        return rows
    return [{field_map.get(k, k): v for k, v in row.items()} for row in rows]


def assert_rows_date(rows: list[dict], config: ExtConfig, day: _date) -> None:
    """⚠️ 关键防污染契约：响应行的日期必须 == 请求的日期。

    真实教训（TSP 参考实现记录的同类事故）：**确实存在忽略 ``?date=`` 参数、
    永远返回当日数据的接口**。不做这层校验时，逐日回补会把「今天的数据」
    写进历史每一天的日期分区 —— 读出的是整段历史，值与今天完全一样，
    schema 正确、没有报错，静默污染整个时序，且事后无法分辨哪些行是假的。

    因此这里 fail-closed：只要有任何一行显式带的日期与请求日不符，整个分区
    拒绝写入，并给出可操作的原因（接口可能不支持 date_param）。
    """
    want = day.isoformat()
    bad: list[str] = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        raw = row.get(config.date_field)
        if raw is None:
            continue
        got = _day_label(raw)
        if got is None:
            bad.append(str(raw))
        elif got != want:
            bad.append(got)
    if bad:
        raise ExtDateMismatch(
            f"接口返回的 {config.date_field}={sorted(set(bad))[:3]} 与请求日期 {want} "
            f"不一致（接口可能忽略 {config.date_param or 'date'} 参数），已拒绝写入该日分区"
        )


def _day_label(value: object) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:10]:
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

Fetcher = Callable[..., Any]


def _format_date(day: _date, date_format: str) -> str:
    return day.strftime("%Y%m%d") if date_format == "compact" else day.isoformat()


def build_url(config: ExtConfig, day: _date, extra: dict[str, Any] | None = None) -> str:
    """在拉取 URL 上写入日期参数（与可选分页参数）。

    **覆盖**同名 query 而不是追加：配置里的 URL 常常已经带了占位 ``?date=``，
    追加会产生两个同名参数，服务端取哪个完全看实现 —— 回补分区的日期与
    实际请求日期不一致，正是最难查的那种静默污染。
    """
    assert config.pull is not None
    url = config.pull.url
    params: dict[str, Any] = {}
    if config.date_param:
        params[config.date_param] = _format_date(day, config.date_format)
    params.update(extra or {})
    if not params:
        return url
    parts = urlsplit(url)
    merged = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged.update({k: str(v) for k, v in params.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(merged), parts.fragment))


def _default_fetcher(url: str, *, method: str, headers: dict, body: str | None,
                     timeout: int) -> Any:
    """标准库 HTTP 拉取（见模块头：核心安装没有 httpx）。"""
    import urllib.request

    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None and not any(k.lower() == "content-type" for k in headers):
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 配置里的用户 URL
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def fetch_rows(
    config: ExtConfig, day: _date, *, fetcher: Fetcher | None = None
) -> list[dict]:
    """按日拉取并解析为行（**不写盘**）。空数据返回 ``[]``。

    解析完成后立刻过日期校验 —— 校验必须发生在写盘之前，否则「先写后校验」
    在异常路径上就会留下脏数据。
    """
    pull = config.pull
    if pull is None or not pull.url:
        raise ExtConfigError(f"扩展表 {config.id} 未配置拉取 URL")
    fetch = fetcher or _default_fetcher

    extra: dict[str, Any] = {}
    page = 1
    collected: list[dict] = []
    while True:
        if pull.page_param:
            extra = {pull.page_param: page}
            if pull.page_size_param and pull.page_size > 0:
                extra[pull.page_size_param] = pull.page_size
        url = build_url(config, day, extra)
        try:
            payload = fetch(
                url, method=pull.method, headers=dict(pull.headers),
                body=pull.body, timeout=pull.timeout_seconds,
            )
        except TypeError:
            # 兼容只接受 url 的最简 fetcher（测试与自定义注入常用形态）
            payload = fetch(url)
        page_rows = apply_field_map(extract_rows(payload, pull.response_path), pull.field_map)
        collected.extend(page_rows)
        if not pull.page_param or not page_rows:
            break
        if pull.page_size > 0 and len(page_rows) < pull.page_size:
            break  # 短页 = 最后一页
        page += 1
        if page > max(1, pull.max_pages):
            logger.warning(f"扩展表 {config.id}: 分页达到 max_pages={pull.max_pages} 上限")
            break

    assert_rows_date(collected, config, day)
    return collected


def pull_day(
    config: ExtConfig, day: _date, *, data_root: str | Path | None = None,
    fetcher: Fetcher | None = None,
) -> int:
    """拉取单日并写入（返回写入行数）。"""
    rows = fetch_rows(config, day, fetcher=fetcher)
    if not rows:
        raise ExtConfigError(f"扩展表 {config.id} 在 {day.isoformat()} 拉取到 0 行")
    return write_frame(pl.DataFrame(rows), config, day=day, data_root=data_root)


def backfill(
    config: ExtConfig,
    start: _date,
    end: _date,
    *,
    data_root: str | Path | None = None,
    trading_days: list[_date] | None = None,
    fetcher: Fetcher | None = None,
    max_days: int = MAX_BACKFILL_DAYS,
    sleep_seconds: float = 0.0,
) -> dict:
    """逐交易日回补 timeseries 历史分区（幂等：已有分区跳过）。

    单日失败**不中断**整轮回补，汇总进 ``failed``（日期 + 原因）返回 ——
    一个接口在某个交易日抽风，不该让后面 100 天全部补不上；同时失败必须
    逐项可见，不能被吞成「总体成功 99 天」。
    """
    import time

    if config.mode != "timeseries":
        raise ExtConfigError("只有 timeseries 模式支持历史回补（snapshot 没有历史概念）")
    if config.pull is None or not config.pull.url:
        raise ExtConfigError(f"扩展表 {config.id} 未配置拉取 URL")
    if not config.date_param:
        raise ExtConfigError(
            f"扩展表 {config.id} 未配置 date_param —— 接口不支持按日查询时无法回补"
        )
    if start > end:
        raise ExtConfigError(f"开始日期 {start} 晚于结束日期 {end}")
    if (end - start).days + 1 > max_days:
        raise ExtConfigError(f"单次回补上限 {max_days} 天，请分段执行")

    if trading_days is None:
        from lquant.core.calendar import trade_days as _trade_days

        try:
            trading_days = _trade_days(start, end)
        except Exception as e:  # noqa: BLE001 - 日历表缺失/损坏都要给出可操作原因
            raise ExtConfigError(
                f"读取交易日历失败（{e}）—— 需先同步 trade_calendar，"
                f"或显式传入 trading_days"
            ) from e
    days = sorted({d for d in trading_days if start <= d <= end})
    if not days:
        raise ExtConfigError(
            "区间内没有交易日（需先在 DuckDB 里同步 trade_calendar，或显式传入 trading_days）"
        )

    have = set(existing_dates(config, data_root))
    fetched = skipped = empty = 0
    rows_written = 0
    failed: list[dict] = []
    for i, day in enumerate(days):
        if day.isoformat() in have:
            skipped += 1
            continue
        try:
            rows = fetch_rows(config, day, fetcher=fetcher)
            if not rows:
                empty += 1  # 该日无数据（服务端未归档）不是错误
            else:
                rows_written += write_frame(
                    pl.DataFrame(rows), config, day=day, data_root=data_root
                )
                fetched += 1
        except Exception as e:  # noqa: BLE001 - 单日失败逐项记录，不中断
            failed.append({"date": day.isoformat(), "reason": str(e)[:300]})
        if sleep_seconds and i + 1 < len(days):
            time.sleep(sleep_seconds)  # 限速，对数据源礼貌
    return {
        "total_days": len(days), "fetched": fetched, "skipped_existing": skipped,
        "empty": empty, "failed": failed, "rows_written": rows_written,
    }


# ---------------------------------------------------------------------------
# 文件上传 / JSON
# ---------------------------------------------------------------------------

def _rename_aliases(df: pl.DataFrame, config: ExtConfig) -> pl.DataFrame:
    """把常见中文/英文列名别名归一到配置字段名。"""
    renames: dict[str, str] = {}
    names = set(df.columns)
    if config.symbol_field and config.symbol_field not in names:
        for alias in _SYMBOL_ALIASES:
            if alias in names:
                renames[alias] = config.symbol_field
                break
    if config.date_field not in names:
        for alias in _DATE_ALIASES:
            if alias in names:
                renames[alias] = config.date_field
                break
    return df.rename(renames) if renames else df


def parse_upload(path: Path, config: ExtConfig) -> pl.DataFrame:
    """解析上传的 CSV / Excel（编码归一 + 列名归一）。"""
    # 代码/日期列强制按字符串读：CSV 里 "000001" 被推断成整数会变成 1，
    # 前导零直接丢失，标的就再也对不上日线面板（这类静默错位最难查）。
    text_columns = {c: pl.Utf8 for c in (*_SYMBOL_ALIASES, *_DATE_ALIASES)}
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pl.read_csv(
            ensure_utf8_csv(path), infer_schema_length=10000, schema_overrides=text_columns
        )
    elif suffix in (".xlsx", ".xls"):
        df = _read_excel(path, text_columns)
    else:
        raise ExtConfigError(f"不支持的文件格式: {suffix}（仅支持 .csv/.xlsx/.xls）")
    return _rename_aliases(clean_column_names(df), config)


def _read_excel(path: Path, text_columns: dict[str, object]) -> pl.DataFrame:
    """Excel → DataFrame（polars 优先，缺 fastexcel 时退到 pandas/openpyxl）。

    polars 的 ``read_excel`` 依赖可选的 ``fastexcel``；本仓库核心依赖里只有
    pandas。两条引擎都不可用时显式报出「装哪个包能解决」，而不是把底层
    「module not found」原样抛给用户。
    """
    try:
        return pl.read_excel(path, schema_overrides=text_columns)
    except ImportError:
        pass
    except TypeError:
        # 老版本 polars 的 read_excel 不认 schema_overrides
        try:
            return pl.read_excel(path)
        except ImportError:
            pass
    try:
        import pandas as pd

        return pl.from_pandas(pd.read_excel(path, dtype={k: str for k in text_columns}))
    except ImportError as e:
        raise ExtConfigError(
            f"Excel 解析需要 fastexcel 或 openpyxl/pandas 之一，当前均不可用: {e}"
        ) from e


def write_json_rows(
    config: ExtConfig,
    rows: list[dict],
    *,
    day: _date | None = None,
    data_root: str | Path | None = None,
) -> int:
    """JSON 行写入（API / 函数直写）。

    逐行校验必填字段：缺列时报出**第几行缺哪个字段**，而不是让 polars 在
    后面构造 DataFrame 时抛一句看不出上下文的错误。
    """
    if not rows:
        raise ExtConfigError("rows 为空，无可写入数据")
    required = [f.name for f in config.fields]
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ExtConfigError(f"第 {i + 1} 行不是对象: {type(row)}")
        if config.symbol_field and config.symbol_field not in row:
            raise ExtConfigError(f"第 {i + 1} 行缺少标的字段 {config.symbol_field!r}")
        missing = [f for f in required if f not in row]
        if missing:
            raise ExtConfigError(f"第 {i + 1} 行缺少字段: {', '.join(missing)}")
    df = pl.DataFrame(rows)
    if config.mode == "snapshot" and day is None:
        day = _date.today()
    return write_frame(df, config, day=day, data_root=data_root)


def import_file(
    config: ExtConfig,
    path: str | Path,
    *,
    day: _date | None = None,
    data_root: str | Path | None = None,
) -> int:
    """按后缀导入 CSV/Excel/JSON 文件。

    JSON 支持两种形态：``[{...}, ...]`` 行数组，或 ``{"rows": [...], "date": "..."}``
    —— 后者与 API 的写入请求体同形，用户能直接把抓好的响应存盘再导入。
    """
    p = Path(path)
    if not p.exists():
        raise ExtConfigError(f"文件不存在: {p}")
    if p.suffix.lower() == ".json":
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = payload.get("rows") or []
            if payload.get("date") and day is None:
                day = _date.fromisoformat(str(payload["date"]))
        elif isinstance(payload, list):
            rows = payload
        else:
            raise ExtConfigError(f"JSON 顶层必须是数组或 {{rows:[...]}} 对象: {type(payload)}")
        return write_json_rows(config, rows, day=day, data_root=data_root)

    df = parse_upload(p, config)
    if config.mode == "snapshot" and day is None:
        day = _date.today()
    if config.mode == "timeseries" and day is None:
        raise ExtConfigError(
            f"扩展表 {config.id} 是 timeseries 模式，导入必须指定 --date（或 JSON 里带 date）"
        )
    return write_frame(df, config, day=day, data_root=data_root)
